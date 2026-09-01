"""История Слюши: переживает перезапуск, ловит вложения, чистит хвост."""
import asyncio
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-hist-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db, history      # noqa: E402

CID = -100555
FAILS = []
SENT = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    async def me(self):
        return SimpleNamespace(id=1000, username="slusha_bot", full_name="Слюша")

    async def send_chat_action(self, *a, **kw):
        return True

    async def send_message(self, *a, **kw):
        return SimpleNamespace(message_id=1)


def msg(text=None, caption=None, **media):
    base = dict(chat=SimpleNamespace(id=CID, title="Чат", type="supergroup"),
                from_user=SimpleNamespace(id=7, username="vasya", full_name="Вася",
                                          is_bot=False),
                text=text, caption=caption, message_id=1, reply_to_message=None,
                sticker=None, photo=None, animation=None, video=None, voice=None,
                video_note=None, audio=None, document=None, poll=None, dice=None,
                location=None, venue=None, contact=None, game=None)
    base.update(media)
    return SimpleNamespace(**base)


async def fake_ollama(system, messages, tokens=0, images=None):
    SENT.append(messages)
    return "ответ"


async def main():
    check("база переписки отдельная", config.HISTORY_DB != config.DB_PATH)
    check("и лежит рядом с временной", config.HISTORY_DB.startswith(TMP))

    await db.init()
    await db.upsert_chat(CID, "Чат", None, 424211817)
    await db.set_setting(CID, "ai_on", 1)
    s = await db.get_settings(CID)
    check("окно контекста по умолчанию как в config",
          s.ai_ctx == config.AI_CTX_DEFAULT)
    check("и это не полсотни: небольшая модель столько уже размазывает",
          config.AI_CTX_DEFAULT <= 30)
    check("буфер памяти не меньше самого большого окна",
          config.AI_HISTORY >= max(config.AI_CTX_PRESETS))

    # --- 1. переписка переживает перезапуск ---
    await ai.remember(CID, "@vasya", "первое")
    await ai.remember(CID, ai.SELF, "второе")
    ai._history.clear()                      # как будто процесс перезапустили
    ai._loaded.clear()
    rows = await ai.history(CID, 50)
    check("история поднялась из базы", [r[1] for r in rows] == ["первое", "второе"])
    check("автор сохранился", rows[1][0] == ai.SELF)

    # --- 2. вложения ---
    bot = FakeBot()
    ai._ask_ollama = fake_ollama
    cases = [
        (msg(sticker=SimpleNamespace(emoji="😀")), "[стикер 😀]"),
        (msg(photo=[object()]), "[фото]"),
        (msg(voice=object()), "[голосовое]"),
        (msg(video_note=object()), "[кружок]"),
        (msg(animation=object()), "[гифка]"),
        (msg(document=SimpleNamespace(file_name="смета.pdf")), "[файл: смета.pdf]"),
        (msg(poll=SimpleNamespace(question="пиво?")), "[опрос: пиво?]"),
        (msg(dice=SimpleNamespace(emoji="🎲", value=6)), "[кубик 🎲: 6]"),
    ]
    for m, want in cases:
        check(f"подпись {want}", ai.attachment_label(m) == want)
    check("служебное событие пропускаем", ai.attachment_label(msg()) == "")

    before = len(SENT)
    await ai.maybe_reply(bot, msg(photo=[object()]), s)
    check("на голое вложение бот не отвечает", len(SENT) == before)
    check("но в историю оно попало", (await ai.history(CID, 1))[0][1] == "[фото]")

    # --- 3. чистка хвоста пачками ---
    for i in range(history.KEEP + history.PRUNE_EVERY + 5):
        await ai.remember(CID, "@vasya", f"строка {i}")
    await history.close()
    con = sqlite3.connect(config.HISTORY_DB)
    left = con.execute("SELECT COUNT(*) FROM ai_history WHERE chat_id=?", (CID,)).fetchone()[0]
    con.close()
    check(f"в базе не копится лишнее (осталось {left})", left <= history.KEEP + history.PRUNE_EVERY)

    # --- 4. «забыть переписку» чистит и базу ---
    wiped = await ai.forget(CID)
    check("забыли не ноль", wiped > 0)
    check("в памяти пусто", await ai.history(CID, 50) == [])
    await history.close()
    con = sqlite3.connect(config.HISTORY_DB)
    left = con.execute("SELECT COUNT(*) FROM ai_history WHERE chat_id=?", (CID,)).fetchone()[0]
    con.close()
    check("и в базе пусто", left == 0)

    # --- 4b. одновременное первое обращение к базе ---
    # Реакции в чате прилетают пачкой, и каждая лезет в базу своим хендлером.
    # Без замка все они видели «соединения нет», открывали по своему и делали
    # PRAGMA поверх чужой транзакции: SQLite отвечал «Safety level may not be
    # changed inside a transaction», реакция терялась, в лог сыпались
    # трейсбеки. Ровно это и было видно в боевом логе.
    await history.close()
    results = await asyncio.gather(
        *[history.tail(CID, 5) for _ in range(12)], return_exceptions=True)
    beda = [r for r in results if isinstance(r, Exception)]
    check(f"пачка одновременных обращений не падает ({len(beda)} ошибок)", not beda)
    check("соединение при этом одно", history._db is not None)

    # --- 5. миграция окна контекста ---
    await db.set_setting(CID, "ai_ctx", 20)          # как было у старых чатов
    await db.kv_set("mig_ctx50", None)               # флаг снят — миграция повторится
    other = -100556
    await db.upsert_chat(other, "Второй", None, 424211817)
    await db.set_setting(other, "ai_ctx", 30)        # осознанный выбор человека
    await db._migrate()
    check("старый дефолт подняли до 50", (await db.get_settings(CID)).ai_ctx == 50)
    check("чужой выбор не тронули", (await db.get_settings(other)).ai_ctx == 30)
    await db.set_setting(CID, "ai_ctx", 20)
    await db._migrate()                              # флаг уже стоит
    check("повторно миграция не срабатывает", (await db.get_settings(CID)).ai_ctx == 20)

    await history.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
