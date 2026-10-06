"""Меню бота-собеседника. Всё живёт в одном сообщении, как у модератора.

Разделов тут мало: чаты, настройки разума на чат и список допуска.
Модерации нет вовсе — этот бот только разговаривает.
"""
import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import ai, card as cards, config, db, history as store, schema, utils

logger = logging.getLogger("slusha.menu")


# Предел одного сообщения в Telegram — в единицах UTF-16: эмодзи идёт за два.
TG_LIMIT = 4096


def _tg_len(text: str) -> int:
    """Длина текста так, как её считает Telegram — без разметки, в UTF-16."""
    visible = re.sub(r"<[^>]+>", "", text)
    return len(visible.encode("utf-16-le")) // 2


def _fit_lines(text: str, room: int) -> str:
    """Столько целых строк текста, сколько влезет в room знаков Telegram."""
    out, used = [], 0
    for line in text.splitlines():
        size = _tg_len(line) + 1
        if used + size > room:
            break
        out.append(line)
        used += size
    return "\n".join(out).rstrip()

router = Router()
router.message.filter(F.chat.type == "private")

CHATS_PER_PAGE = 8

_HOME = ("<b>🧠 Слюша</b>\n\nБот-собеседник. Добавьте его в чат, включите разум "
         "и настройте характер.")


class Input(StatesGroup):
    persona = State()     # ждём текст характера или файл карточки
    names = State()       # ждём имена-обращения
    examples = State()    # ждём примеры реплик
    access = State()      # ждём id/@username для доступа
    forget_people = State()  # ждём ники, кого забыть


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------- вьюхи ----------

async def view_home(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    b = InlineKeyboardBuilder()
    b.button(text="💬 Чаты", callback_data="m:chats:0")
    if config.WEBAPP_URL:
        # мини-приложение Telegram открывается только по https, поэтому без
        # публичного адреса кнопку не показываем вовсе
        b.row(InlineKeyboardButton(text="🌐 Панель",
                                   web_app=WebAppInfo(url=config.WEBAPP_URL)))
    if user_id in config.ADMIN_IDS:
        b.button(text="👥 Доступ к боту", callback_data="m:acc")
    b.button(text="✖️ Закрыть", callback_data="m:close")
    b.adjust(1)
    return _HOME, b.as_markup()


async def view_chats(bot: Bot, viewer_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    chats = await db.chats_for(viewer_id)
    pages = max(1, -(-len(chats) // CHATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    me = await bot.me()

    b = InlineKeyboardBuilder()
    if chats:
        text = f"<b>💬 Чаты</b> ({len(chats)})"
        if pages > 1:
            text += f" · страница {page + 1} из {pages}"
        text += "\n\nВыберите чат — откроются настройки разума."
        for c in chats[page * CHATS_PER_PAGE:(page + 1) * CHATS_PER_PAGE]:
            s = await db.get_settings(c["chat_id"])
            mark = "✅" if s.ai_on else "🚫"
            b.row(_btn(f"{mark} {(c['title'] or c['chat_id'])}"[:60], f"m:c:{c['chat_id']}"))
    else:
        text = ("<b>💬 Чаты</b>\n\nПока пусто. Добавьте бота в свой чат — "
                "он появится здесь.")
    if pages > 1:
        b.row(_btn("◀", f"m:chats:{(page - 1) % pages}"),
              _btn(f"{page + 1}/{pages}", f"m:chats:{page}"),
              _btn("▶", f"m:chats:{(page + 1) % pages}"))
    b.row(InlineKeyboardButton(text="➕ Добавить в чат",
                               url=f"https://t.me/{me.username}?startgroup=true"))
    b.row(_btn("⬅️ Назад", "m:home"))
    return text, b.as_markup()


async def view_chat(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка чата: все настройки разума и всё, что к нему прилагается."""
    ch = await db.get_chat(cid)
    s = await db.get_settings(cid)
    spent = await ai.spent_today(cid)
    limit = f"из {s.ai_daily}" if ai.capped() else "без лимита"
    # длину показываем всегда: характер уезжает в модель на каждый запрос
    # и делит окно с историей, а карточки с chub бывают на пять тысяч знаков
    size = len(s.ai_persona or "")
    persona = f"{size} знаков" if s.ai_persona else "по умолчанию"
    if size > config.AI_PERSONA_SOFT:
        persona += " ⚠️"
    ex = len([r for r in (s.ai_examples or "").split("\n") if r.strip()])
    names = len([n for n in (s.ai_names or "").split(",") if n.strip()])
    kept, _ = await store.summary_get(cid)
    folks = await store.people_count(cid)
    notes = f"{len(kept)} знаков" if kept.strip() else "пока нет"
    if folks:
        notes += f", людей: {folks}"

    lines = [
        f"<b>🧠 {utils.esc(ch['title'] if ch else str(cid))}</b>",
        f"<code>{cid}</code>\n",
        schema.INTRO,
        "",
        f"🧬 Мозги: <b>{utils.esc(ai.provider_label())}</b>",
        f"📊 Сегодня ответов: <b>{spent}</b> {limit}",
        "",
    ]
    for f in schema.FIELDS:
        lines.append(f"{f.label}: <b>{schema.value_label(f, getattr(s, f.key))}</b>")

    b = InlineKeyboardBuilder()
    for f in schema.FIELDS:
        if f.kind == "toggle":
            b.row(_btn(f"{schema.value_label(f, getattr(s, f.key))} · {f.label}",
                       f"m:t:{cid}:{f.key}"))
        else:
            b.row(_btn("◀", f"m:y:{cid}:{f.key}:-"),
                  _btn(f"{f.label}: {schema.value_label(f, getattr(s, f.key))}",
                       f"m:y:{cid}:{f.key}:+"),
                  _btn("▶", f"m:y:{cid}:{f.key}:+"))
    b.row(_btn(f"🎭 Характер: {persona}", f"m:persona:{cid}"))
    b.row(_btn(f"💬 Примеры реплик: {ex or 'нет'}", f"m:ex:{cid}"))
    b.row(_btn(f"🔔 Имена-обращения: {names}", f"m:names:{cid}"))
    b.row(_btn(f"🧠 Заметки о чате: {notes}", f"m:sum:{cid}"))
    b.row(_btn("🧹 Забыть переписку", f"m:forget:{cid}"))
    b.row(InlineKeyboardButton(text="🚪 Убрать бота из чата",
                               callback_data=f"m:leave:{cid}", style="danger"))
    b.row(_btn("⬅️ Назад", "m:chats:0"))
    return "\n".join(lines), b.as_markup()


async def view_notes(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Что бот запомнил о чате сверх окна контекста."""
    from . import memory, summary
    await summary.block(cid)          # старые заметки с людьми внутри — разложить
    text, covered = await store.summary_get(cid)
    # договорённости и шутки живут в долгой памяти: показываем свежие оттуда
    kept = await memory.notes_all(cid) if memory.notes_on() else []
    if kept:
        text = summary._fill(text, {k: [r["text"] for r in kept if r["kind"] == k][:8]
                                    for k in memory.NOTE_KINDS.values()})
    people = await store.people_all(cid)
    left = await store.pending(cid, covered)
    when = await store.summary_updated(cid)
    lines = [
        "<b>🧠 Заметки о чате</b>\n",
        "Окно контекста помнит только последние сообщения. Всё, что уехало "
        "за его край, бот время от времени пересказывает себе сюда: кто есть "
        "кто, о чём договорились, какие шутки прижились. Общие заметки уходят "
        "в каждый запрос как справка, а из людей — только те, кто сейчас "
        f"в разговоре (до {config.AI_PROMPT_PEOPLE}).\n",
        (f"Обновлено: <b>{utils.stamp(when)}</b>\n" if when else ""),
        f"Новых сообщений с прошлой пересборки: <b>{left}</b> "
        f"(пересобирает каждые {config.AI_SUMMARY_EVERY}, потолок заметок — "
        f"{config.AI_SUMMARY_LIMIT} знаков).\n",
    ]
    if kept:
        deals = sum(r["kind"] == "deal" for r in kept)
        lines.append(f"Договорённости и шутки копятся в долгой памяти: <b>{deals}</b> и "
                     f"<b>{len(kept) - deals}</b>. В запрос идут только подходящие к "
                     f"разговору (до {config.MEM_NOTES_SHOWN}), здесь — свежие.\n")
    # Кого бот держит за грубияна — самым верхом: с ними он говорит холодно,
    # и если человек исправился, его видно и можно забыть.
    from . import mood
    grumps = [m[3] for m in (await store.mood_all(cid)).values() if mood.rude(m)]
    full = f"ГРУБЯТ БОТУ: {', '.join(grumps)}\n\n" if grumps else ""
    # Незакрытые темы: о чём бот спросит, когда человек ему напишет.
    later = await store.plans_open(cid)
    if later:
        full += "СПРОСИТ, КАК ПРОШЛО:\n" + "\n".join(
            f"— {w} — «{t[:80]}» (с {utils.stamp(ts)})" for w, t, ts in later) + "\n\n"
    # Люди — первыми и свежие вперёд: их записи меняются чаще всего.
    if people:
        full += f"ЛЮДИ ({len(people)}):\n" + "\n".join(f"— {w} — {d}" for w, d in people)
    if text.strip():
        full = (full + "\n\n" + text.strip()).strip()
    if full:
        # Экран — одно сообщение, а заметки с лимитом 4000 знаков вместе с
        # шапкой в него не влезали: Telegram отказывал, и кнопка «Заметки»
        # молча переставала открываться. Показываем столько целых строк,
        # сколько помещается; сами заметки при этом не трогаем.
        room = TG_LIMIT - _tg_len("\n".join(lines)) - 120
        shown = _fit_lines(full, room)
        lines.append(f"<i>{utils.esc(shown)}</i>")
        if len(shown) < len(full):
            lines.append(f"\n<i>… показано {len(shown)} из {len(full)} знаков. "
                         "Бот при этом помнит заметки целиком.</i>")
    else:
        lines.append("Пока пусто — бот ещё не набрал материала.")

    b = InlineKeyboardBuilder()
    if full:
        b.row(_btn("🧽 Забыть людей", f"m:sumppl:{cid}"))
    if full:
        b.row(InlineKeyboardButton(text="🗑 Очистить заметки",
                                   callback_data=f"m:sumclr:{cid}", style="danger"))
    b.row(_btn("⬅️ Назад", f"m:c:{cid}"))
    return "\n".join(lines), b.as_markup()


async def view_access() -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.access_list()
    b = InlineKeyboardBuilder()
    text = ("<b>👥 Доступ к боту</b>\n\n"
            "Кому разрешено звать бота в чаты и настраивать его. "
            "Добавляйте по числовому id или @username.\n"
            f"Записей: <b>{len(rows)}</b>")
    for r in rows:
        who = await db.user_label(r["user_id"], r["username"])
        b.row(_btn(f"❌ {who}"[:40], f"m:accd:{r['id']}"))
    b.row(_btn("➕ Добавить", "m:acca"))
    b.row(_btn("⬅️ Назад", "m:home"))
    return text, b.as_markup()


# ---------- вспомогательное для FSM ----------

async def _ask(cb: CallbackQuery, state: FSMContext, st, prompt: str,
               back: str, cid: int = 0) -> None:
    """Спросить текст: правим то же сообщение, ответ ждём отдельным сообщением."""
    await state.set_state(st)
    await state.update_data(cid=cid, msg_id=cb.message.message_id, back=back)
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Отмена", back))
    await cb.message.edit_text(prompt + "\n\n<i>Отмена — /cancel.</i>",
                               reply_markup=b.as_markup())
    await cb.answer()


async def _finish(message: Message, bot: Bot, state: FSMContext,
                  view: tuple[str, InlineKeyboardMarkup], note: str = "") -> None:
    """Вернуть меню на место: правим исходное сообщение, ответ юзера убираем."""
    data = await state.get_data()
    await state.clear()
    text, kb = view
    try:
        await bot.edit_message_text(note + text, chat_id=message.chat.id,
                                    message_id=data.get("msg_id"), reply_markup=kb)
    except Exception:
        await message.answer(note + text, reply_markup=kb)
    try:
        await message.delete()
    except Exception:
        pass


async def _retry(message: Message, bot: Bot, state: FSMContext, prompt: str) -> None:
    data = await state.get_data()
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Отмена", data.get("back", "m:home")))
    try:
        await bot.edit_message_text(prompt + "\n\n<i>Отмена — /cancel.</i>",
                                    chat_id=message.chat.id,
                                    message_id=data.get("msg_id"),
                                    reply_markup=b.as_markup())
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass


async def _greet(bot: Bot, cid: int, text: str) -> None:
    """Поздороваться в чате от лица персонажа и запомнить это как свою реплику."""
    try:
        sent = await bot.send_message(cid, utils.esc(text))
    except Exception:
        logger.warning("не поздороваться в чате %s", cid, exc_info=True)
        return
    await ai.remember(cid, ai.SELF, text, getattr(sent, "message_id", None))


async def _guard(cb: CallbackQuery, cid: int) -> bool:
    if await db.owns_chat(cb.from_user.id, cid):
        return True
    await cb.answer("Это не ваш чат.", show_alert=True)
    return False


async def _show(cb: CallbackQuery, view: tuple[str, InlineKeyboardMarkup],
                note: str = "") -> None:
    text, kb = view
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        # «Сообщение не изменилось» — нормально, нажали ту же кнопку ещё раз.
        # А всё прочее раньше глоталось так же молча: экран заметок перестал
        # открываться из-за длины, и в логе не было ни строчки.
        if "not modified" not in str(e):
            logger.warning("меню: экран не показан: %s", e)
            note = note or "Не получилось показать этот экран."
    await cb.answer(note)


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = await view_home(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
    if await state.get_state() is None:
        return
    data = await state.get_data()
    cid = data.get("cid") or 0
    view = await (view_chat(cid) if cid else view_home(message.from_user.id))
    await _finish(message, bot, state, view)


# ---------- навигация ----------

@router.callback_query(F.data == "m:home")
async def cb_home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(cb, await view_home(cb.from_user.id))


@router.callback_query(F.data.startswith("m:chats:"))
async def cb_chats(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await _show(cb, await view_chats(bot, cb.from_user.id, int(cb.data.split(":")[2])))


@router.callback_query(F.data == "m:close")
async def cb_close(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        await cb.answer("Не смог удалить — уберите вручную.", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.startswith("m:c:"))
async def cb_chat(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    await _show(cb, await view_chat(cid))


# ---------- поля ----------

@router.callback_query(F.data.startswith("m:t:"))
async def cb_toggle(cb: CallbackQuery, bot: Bot) -> None:
    _, _, cid, key = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    f = schema.BY_KEY.get(key)
    if f is None or f.kind != "toggle":
        return
    s = await db.get_settings(cid)
    was = getattr(s, key)
    await db.set_setting(cid, key, 0 if was else 1)
    if key == "ai_on" and not was and s.ai_greeting:
        # first_mes карточки: персонаж здоровается сам, как только его включили
        await _greet(bot, cid, s.ai_greeting)
    await _show(cb, await view_chat(cid))


@router.callback_query(F.data.startswith("m:y:"))
async def cb_cycle(cb: CallbackQuery) -> None:
    _, _, cid, key, sign = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    f = schema.BY_KEY.get(key)
    if f is None or f.kind != "cycle":
        return
    s = await db.get_settings(cid)
    await db.set_setting(cid, key, schema.cycle(f, getattr(s, key), 1 if sign == "+" else -1))
    await _show(cb, await view_chat(cid))


@router.callback_query(F.data.startswith("m:sum:"))
async def cb_notes(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    await _show(cb, await view_notes(cid))


@router.callback_query(F.data.startswith("m:sumclr:"))
async def cb_notes_clear(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    from . import summary
    await summary.clear(cid)
    await _show(cb, await view_notes(cid), "Заметки очищены")


@router.callback_query(F.data.startswith("m:sumppl:"))
async def cb_notes_people(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.forget_people,
        "<b>🧽 Забыть людей</b>\n\nПришлите ники тех, кого бот должен забыть: "
        "через пробел, запятую или с новой строки — <code>@vasya, @petya</code>. "
        "У кого нет юзернейма — имя целиком, как в заметках, каждое с новой "
        "строки.\n\nСотрётся запись о человеке, его строки в общих заметках и "
        "счёт грубостей боту. Переписка в окне контекста останется.",
        f"m:sum:{cid}", cid=cid,
    )


def _nicks(text: str) -> list[str]:
    """Ники из ввода: «@a @b, @c» и полные имена без юзернейма по строке."""
    out = []
    for chunk in re.split(r"[,;\n]+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "@" in chunk:
            out += [w for w in chunk.split() if w.strip("@")]
        else:
            out.append(chunk)
    return out


@router.message(StateFilter(Input.forget_people))
async def notes_people_input(message: Message, state: FSMContext, bot: Bot) -> None:
    cid = (await state.get_data())["cid"]
    whos = _nicks(message.text or "")
    if not whos:
        await _retry(message, bot, state,
                     "<b>🧽 Забыть людей</b>\n\n⚠️ Пришлите хотя бы один ник.")
        return
    found = await store.people_forget(cid, whos)
    missed = [w for w in whos if w.strip() not in found]
    note = (f"✅ Забыты: {utils.esc(', '.join(found))}.\n" if found else "")
    if missed:
        note += f"🤷 Не нашёл: {utils.esc(', '.join(missed))}.\n"
    await _finish(message, bot, state, await view_notes(cid), note + "\n")


@router.callback_query(F.data.startswith("m:forget:"))
async def cb_forget(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    n = await ai.forget(cid)
    await cb.answer(f"Забыто реплик: {n}", show_alert=True)


@router.callback_query(F.data.startswith("m:leave:"))
async def cb_leave(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    try:
        await bot.leave_chat(cid)
    except Exception as e:
        await cb.answer(f"Не вышло: {e}", show_alert=True)
        return
    await db.set_chat_active(cid, False)
    await _show(cb, await view_chats(bot, cb.from_user.id), "Бот вышел из чата")


# ---------- характер ----------

@router.callback_query(F.data.startswith("m:persona:"))
async def cb_persona(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    cur = utils.esc(s.ai_persona or config.AI_PERSONA_DEFAULT)
    await _ask(
        cb, state, Input.persona,
        "<b>🎭 Характер</b>\n\nОпишите, кто такой бот и как он говорит — это "
        "уходит модели как инструкция.\n"
        "Можно прислать <b>файл карточки</b> с chub.ai (JSON или PNG): возьму "
        "описание, имя, примеры реплик и приветствие.\n"
        f"<b>Длина важна.</b> Характер уезжает в модель на каждый запрос и "
        f"делит окно с историей чата: на пять тысяч знаков он занимает половину "
        f"промпта, и вопрос собеседника в нём тонет. Рабочий размер — до "
        f"{config.AI_PERSONA_SOFT} знаков: кто он и как говорит. Внешность, "
        f"биографию и таблицы атрибутов на реплику в чате можно не описывать.\n"
        "Вернуть характер по умолчанию — пришлите <code>-</code>.\n\n"
        f"Сейчас ({len(s.ai_persona or '')} знаков):\n{cur[:2000]}",
        f"m:c:{cid}", cid=cid,
    )


@router.message(StateFilter(Input.persona))
async def persona_input(message: Message, state: FSMContext, bot: Bot) -> None:
    cid = (await state.get_data())["cid"]
    doc = message.document
    if doc is not None:
        if doc.file_size and doc.file_size > 5 * 1024 * 1024:
            await _retry(message, bot, state,
                         "<b>🎭 Характер</b>\n\n⚠️ Файл больше 5 МБ, это не карточка.")
            return
        buf = await bot.download(doc.file_id)
        result = cards.load(buf.read())
        if result.get("error"):
            await _retry(message, bot, state, f"<b>🎭 Характер</b>\n\n⚠️ {result['error']}")
            return
        card = result.get("card") or {}
        if not card.get("persona"):
            await _retry(message, bot, state,
                         "<b>🎭 Характер</b>\n\n⚠️ В файле нет описания персонажа.")
            return
        done = await cards.apply_card(cid, card)
        note = "✅ Взято из карточки: " + ", ".join(done) + "."
        await _finish(message, bot, state, await view_chat(cid), note + "\n\n")
        return

    text = (message.text or "").strip()
    if text:
        await db.set_setting(cid, "ai_persona",
                             None if text == "-" else text[:config.AI_PERSONA_LIMIT])
    await _finish(message, bot, state, await view_chat(cid))


@router.callback_query(F.data.startswith("m:ex:"))
async def cb_examples(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    cur = (f"\n\nСейчас:\n<code>{utils.esc(s.ai_examples)}</code>"
           if s.ai_examples else "\n\nСейчас пусто.")
    await _ask(
        cb, state, Input.examples,
        "<b>💬 Примеры реплик</b>\n\nНесколько строк того, как персонаж "
        "разговаривает. Уходят в начало переписки как образец манеры: модель "
        "копирует показанное охотнее, чем описанное словами, и на небольших "
        "моделях это работает сильнее, чем страница описания в характере.\n"
        "Формат — по строке на реплику:\n"
        "<code>собеседник: почём яблоки?\nты: дороже, чем вчера. Жизнь такая.</code>\n"
        "Из карточки chub.ai берутся сами, если там заполнено mes_example.\n"
        "Убрать — пришлите <code>-</code>." + cur,
        f"m:c:{cid}", cid=cid,
    )


@router.message(StateFilter(Input.examples))
async def examples_input(message: Message, state: FSMContext, bot: Bot) -> None:
    cid = (await state.get_data())["cid"]
    text = (message.text or "").strip()
    if text:
        await db.set_setting(cid, "ai_examples",
                             None if text == "-" else text[:config.AI_EXAMPLE_LIMIT])
    await _finish(message, bot, state, await view_chat(cid))


@router.callback_query(F.data.startswith("m:names:"))
async def cb_names(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    cur = f"\n\nСейчас: <code>{utils.esc(s.ai_names)}</code>" if s.ai_names else ""
    await _ask(
        cb, state, Input.names,
        "<b>🔔 Имена-обращения</b>\n\nСлова, на которые бот отзывается как на своё "
        "имя, через запятую. Например: <code>слюша, слюш</code>.\n"
        "Убрать все — пришлите <code>-</code>." + cur,
        f"m:c:{cid}", cid=cid,
    )


@router.message(StateFilter(Input.names))
async def names_input(message: Message, state: FSMContext, bot: Bot) -> None:
    cid = (await state.get_data())["cid"]
    text = (message.text or "").strip().lower()
    if text:
        await db.set_setting(cid, "ai_names", None if text == "-" else text[:300])
    await _finish(message, bot, state, await view_chat(cid))


# ---------- доступ ----------

@router.callback_query(F.data == "m:acc")
async def cb_access(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await _show(cb, await view_access())


@router.callback_query(F.data == "m:acca")
async def cb_access_add(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await _ask(cb, state, Input.access,
               "<b>👥 Доступ к боту</b>\n\nПришлите числовой <b>id</b> или "
               "<b>@username</b>.", "m:acc")


@router.message(StateFilter(Input.access))
async def access_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    text = (message.text or "").strip()
    if text.lstrip("-").isdigit():
        await db.access_add(int(text), None)
    elif text.startswith("@") and len(text) > 3:
        await db.access_add(None, text)
    else:
        await _retry(message, bot, state,
                     "<b>👥 Доступ к боту</b>\n\n⚠️ Нужен id или @username.")
        return
    await _finish(message, bot, state, await view_access(), "✅ Добавлен.\n\n")


@router.callback_query(F.data.startswith("m:accd:"))
async def cb_access_del(cb: CallbackQuery) -> None:
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await db.access_remove(int(cb.data.split(":")[2]))
    await _show(cb, await view_access(), "Удалено")
