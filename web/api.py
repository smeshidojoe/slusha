"""HTTP-API панели: те же действия, что и в меню бота, только по JSON.

Правило одно: браузеру нельзя доверять. Каждый запрос приносит подпись
Telegram (её проверяет auth), а каждый запрос про чат ещё и сверяется с базой —
владеет ли этот человек этим чатом. Ровно как _guard в меню: id чата приходит
снаружи, и подставить чужой ничего не стоит.

Логику не дублируем: где меню зовёт lore, ai или db, панель зовёт их же.
Иначе два интерфейса неизбежно разъедутся в поведении.
"""
import json
import logging

from aiohttp import web

from .. import ai, config, db, history as store, lore, schema, summary

logger = logging.getLogger("slusha.web.api")

routes = web.RouteTableDef()


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def js(payload, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def uid_of(request) -> int:
    return int(request["user"]["id"])


async def _chat_id(request) -> int:
    """id чата из адреса — с проверкой прав. Чужой чат до обработчика не дойдёт."""
    try:
        cid = int(request.match_info["cid"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="Плохой id чата")
    if not await db.owns_chat(uid_of(request), cid):
        raise web.HTTPForbidden(text="Это не ваш чат")
    return cid


async def _body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Ожидался JSON")
    return data if isinstance(data, dict) else {}


# ---------- чтение ----------

@routes.get("/api/state")
async def state(request: web.Request) -> web.Response:
    uid = uid_of(request)
    chats = []
    for c in await db.chats_for(uid):
        s = await db.get_settings(c["chat_id"])
        chats.append({"chat_id": c["chat_id"], "title": c["title"], "ai_on": s.ai_on})
    return js({
        "admin": uid in config.ADMIN_IDS,
        "provider": ai.provider_label(),
        "ready": ai.available(),
        "chats": chats,
        # описания полей отдаёт сервер: иначе меню и панель разъедутся при
        # первом же новом переключателе
        "fields": [{"key": f.key, "kind": f.kind, "label": f.label,
                    "values": f.values,
                    "labels": {str(k): v for k, v in (f.value_labels or {}).items()}}
                   for f in schema.FIELDS],
    })


@routes.get("/api/chat/{cid}")
async def chat(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    ch = await db.get_chat(cid)
    s = await db.get_settings(cid)
    notes, covered = await store.summary_get(cid)
    return js({
        "chat_id": cid,
        "title": ch["title"] if ch else str(cid),
        "settings": {f.key: getattr(s, f.key) for f in schema.FIELDS},
        "persona": s.ai_persona or "",
        "persona_default": config.AI_PERSONA_DEFAULT,
        "names": s.ai_names or "",
        "greeting": s.ai_greeting or "",
        "spent": await ai.spent_today(cid),
        "capped": ai.capped(),
        "notes": notes,
        "notes_pending": await store.pending(cid, covered),
        "notes_updated": await store.summary_updated(cid),
        "notes_limit": config.AI_SUMMARY_LIMIT,
        "lore": [dict(r) for r in await db.lore_list(cid)],
    })


# ---------- правка ----------

@routes.post("/api/chat/{cid}/set")
async def set_field(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    data = await _body(request)
    field = schema.BY_KEY.get(data.get("key"))
    if field is None:
        raise web.HTTPBadRequest(text="Неизвестное поле")
    value = data.get("value")
    if field.kind == "toggle":
        value = 1 if value else 0
    elif field.values is not None and value not in field.values:
        raise web.HTTPBadRequest(text="Недопустимое значение")
    was = getattr(await db.get_settings(cid), field.key)
    await db.set_setting(cid, field.key, value)
    if field.key == "ai_on" and value and not was:
        await _greet(request, cid)
    return js({"ok": True, "value": value})


async def _greet(request, cid: int) -> None:
    """Приветствие из карточки — тем же путём, что и при включении из меню."""
    s = await db.get_settings(cid)
    if not s.ai_greeting:
        return
    try:
        sent = await request.app["bot"].send_message(cid, s.ai_greeting)
    except Exception:
        logger.warning("панель: не поздороваться в чате %s", cid, exc_info=True)
        return
    await ai.remember(cid, ai.SELF, s.ai_greeting, getattr(sent, "message_id", None))


@routes.post("/api/chat/{cid}/persona")
async def persona(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    text = (await _body(request)).get("text") or ""
    text = text.strip()[:config.AI_PERSONA_LIMIT]
    await db.set_setting(cid, "ai_persona", text or None)
    return js({"ok": True, "len": len(text)})


@routes.post("/api/chat/{cid}/names")
async def names(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    text = ((await _body(request)).get("text") or "").strip().lower()[:300]
    await db.set_setting(cid, "ai_names", text or None)
    return js({"ok": True})


@routes.post("/api/chat/{cid}/lore")
async def lore_add(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    data = await _body(request)
    content = (data.get("content") or "").strip()[:1500]
    if not content:
        raise web.HTTPBadRequest(text="Пустая запись")
    keys = (data.get("keys") or "").strip()[:300]
    always = 1 if keys in ("", "*") else 0
    row_id = await db.lore_add(cid, "" if always else keys, content, always)
    return js({"ok": True, "id": row_id})


@routes.delete("/api/chat/{cid}/lore/{rid}")
async def lore_del(request: web.Request) -> web.Response:
    await _chat_id(request)
    try:
        rid = int(request.match_info["rid"])
    except ValueError:
        raise web.HTTPBadRequest(text="Плохой id записи")
    await db.lore_remove(rid)
    return js({"ok": True})


@routes.post("/api/chat/{cid}/lore/clear")
async def lore_clear(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    return js({"ok": True, "dropped": await db.lore_clear(cid)})


@routes.post("/api/chat/{cid}/upload")
async def upload(request: web.Request) -> web.Response:
    """Файл с chub.ai: и книга лора, и карточка персонажа — как в меню."""
    cid = await _chat_id(request)
    reader = await request.multipart()
    field = await reader.next()
    if field is None:
        raise web.HTTPBadRequest(text="Файла нет")
    raw = await field.read(decode=False)
    if len(raw) > config.WEB_UPLOAD_MAX:
        raise web.HTTPBadRequest(text="Файл больше 5 МБ, это не карточка")
    result = await lore.import_file(cid, raw)
    if result.get("error"):
        raise web.HTTPBadRequest(text=result["error"])
    done = await lore.apply_card(cid, result.get("card") or {})
    return js({"ok": True, "entries": result["entries"], "card": done})


@routes.post("/api/chat/{cid}/forget")
async def forget(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    return js({"ok": True, "wiped": await ai.forget(cid)})


@routes.post("/api/chat/{cid}/notes/clear")
async def notes_clear(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    await summary.clear(cid)
    return js({"ok": True})


@routes.post("/api/chat/{cid}/leave")
async def leave(request: web.Request) -> web.Response:
    cid = await _chat_id(request)
    try:
        await request.app["bot"].leave_chat(cid)
    except Exception as e:
        raise web.HTTPBadRequest(text=f"Не вышло: {e}")
    await db.set_chat_active(cid, False)
    return js({"ok": True})


# ---------- доступ к боту (только владелец) ----------

def _admin(request) -> None:
    if uid_of(request) not in config.ADMIN_IDS:
        raise web.HTTPForbidden(text="Нет доступа")


@routes.get("/api/access")
async def access_list(request: web.Request) -> web.Response:
    _admin(request)
    rows = []
    for r in await db.access_list():
        rows.append({"id": r["id"], "who": await db.user_label(r["user_id"], r["username"])})
    return js({"rows": rows})


@routes.post("/api/access")
async def access_add(request: web.Request) -> web.Response:
    _admin(request)
    text = ((await _body(request)).get("who") or "").strip()
    if text.lstrip("-").isdigit():
        await db.access_add(int(text), None)
    elif text.startswith("@") and len(text) > 3:
        await db.access_add(None, text)
    else:
        raise web.HTTPBadRequest(text="Нужен id или @username")
    return js({"ok": True})


@routes.delete("/api/access/{rid}")
async def access_del(request: web.Request) -> web.Response:
    _admin(request)
    try:
        rid = int(request.match_info["rid"])
    except ValueError:
        raise web.HTTPBadRequest(text="Плохой id")
    await db.access_remove(rid)
    return js({"ok": True})
