"""Веб-панель: подпись Telegram и права на чат.

Панель ходит в ту же базу, что и меню, и id чата приходит с браузера — то есть
подставить чужой ничего не стоит. Поэтому проверяем не «страница открылась»,
а ровно две вещи: без подписи внутрь не пускают, и с чужим чатом не пускают
тоже.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import urllib.parse
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-web-")
TOKEN = "123456:AAHtesttoken"
os.environ.update(SLUSHA_BOT_TOKEN=TOKEN, SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from aiohttp.test_utils import TestClient, TestServer      # noqa: E402

from slusha import db                                       # noqa: E402
from slusha.web import auth, server                         # noqa: E402

OWNER = 424211817
STRANGER = 999
CID = -1007777
ALIEN = -1008888
FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


def init_data(uid: int, age: int = 0) -> str:
    """Собрать подписанную строку, как её отдаёт мини-приложение Telegram."""
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAA",
        "user": json.dumps({"id": uid, "username": "mike"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


class FakeBot:
    def __init__(self):
        self.left = []

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)

    async def send_message(self, chat_id, text, **kw):
        return SimpleNamespace(message_id=1)


async def main():
    await db.init()
    await db.upsert_chat(CID, "Овощехранилище", None, OWNER)
    await db.upsert_chat(ALIEN, "Чужой", None, 12345)
    await db.access_add(STRANGER, None)          # допущен к боту, но не к чату

    # --- подпись ---
    good = init_data(OWNER)
    check("своя подпись принимается", (auth.check(good) or {}).get("id") == OWNER)
    check("подделанная отвергается", auth.check(good.replace("AAA", "BBB")) is None)
    check("без hash отвергается",
          auth.check("&".join(p for p in good.split("&")
                              if not p.startswith("hash="))) is None)
    check("протухшая отвергается", auth.check(init_data(OWNER, age=10 ** 7)) is None)
    check("мусор отвергается", auth.check("что-то не то") is None)

    # --- API ---
    app = server.build(FakeBot())
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/api/state")
        check("без подписи API молчит", r.status == 401)

        head = {"X-Init-Data": good}
        r = await client.get("/api/state", headers=head)
        data = await r.json()
        check("состояние отдаётся", r.status == 200)
        check("владелец видит все чаты", len(data["chats"]) == 2)
        check("описания полей приходят с сервера",
              any(f["key"] == "ai_vision" for f in data["fields"]))

        r = await client.get(f"/api/chat/{CID}", headers=head)
        chat = await r.json()
        check("карточка чата отдаётся", r.status == 200 and chat["title"] == "Овощехранилище")
        check("характер целиком, а не куском", "persona" in chat)

        r = await client.post(f"/api/chat/{CID}/set", headers=head,
                              json={"key": "ai_on", "value": True})
        check("переключатель работает", r.status == 200)
        check("и доехал до базы", (await db.get_settings(CID)).ai_on == 1)

        r = await client.post(f"/api/chat/{CID}/set", headers=head,
                              json={"key": "ai_ctx", "value": 999})
        check("значение вне списка не принимается", r.status == 400)
        r = await client.post(f"/api/chat/{CID}/set", headers=head,
                              json={"key": "drop_table", "value": 1})
        check("незнакомое поле не принимается", r.status == 400)

        r = await client.post(f"/api/chat/{CID}/persona", headers=head,
                              json={"text": "Ехидный торговец."})
        check("характер сохраняется", r.status == 200)
        check("и лежит в базе",
              (await db.get_settings(CID)).ai_persona == "Ехидный торговец.")

        r = await client.post(f"/api/chat/{CID}/lore", headers=head,
                              json={"keys": "пиво", "content": "В баре наливают."})
        check("запись лора добавляется", r.status == 200)
        rid = (await r.json())["id"]
        r = await client.delete(f"/api/chat/{CID}/lore/{rid}", headers=head)
        check("и удаляется", r.status == 200 and await db.lore_count(CID) == 0)

        # --- чужой чат ---
        alien = {"X-Init-Data": init_data(STRANGER)}
        r = await client.get(f"/api/chat/{CID}", headers=alien)
        check("чужой чат не показывают", r.status == 403)
        r = await client.post(f"/api/chat/{CID}/set", headers=alien,
                              json={"key": "ai_on", "value": False})
        check("и править его не дают", r.status == 403)
        check("настройка не изменилась", (await db.get_settings(CID)).ai_on == 1)
        r = await client.get("/api/access", headers=alien)
        check("список допуска — только владельцу", r.status == 403)
        r = await client.get("/api/access", headers=head)
        check("владельцу — можно", r.status == 200)

    from slusha import history as store
    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
