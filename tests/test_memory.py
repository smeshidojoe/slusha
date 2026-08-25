"""Заметки о чате и миграции баз.

Половина файла — про то, как новые колонки приезжают в УЖЕ СУЩЕСТВУЮЩУЮ базу.
Проверять это на пустой бесполезно: там всё создаётся сразу правильным, и
ошибка «no such column» вылезает только на боевой. Поэтому здесь база сначала
собирается по старой схеме и наполняется данными, и только потом открывается
рабочим кодом.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="slusha-mem-")
DB = os.path.join(TMP, "t.sqlite3")
HIST = os.path.join(TMP, "t_history.sqlite3")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=DB, SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b", AI_SUMMARY_EVERY="10")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

CID = -100321
FAILS = []
ASKED = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


def build_old_bases():
    """Собрать базы такими, какими они были до этой правки, и налить данных."""
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE chats(chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT,
                           owner_id INTEGER, active INTEGER NOT NULL DEFAULT 1,
                           added_at INTEGER NOT NULL);
        CREATE TABLE settings(chat_id INTEGER PRIMARY KEY,
                              ai_on INTEGER NOT NULL DEFAULT 0, ai_persona TEXT,
                              ai_random INTEGER NOT NULL DEFAULT 3,
                              ai_ctx INTEGER NOT NULL DEFAULT 50,
                              ai_daily INTEGER NOT NULL DEFAULT 100,
                              ai_names TEXT, ai_free INTEGER NOT NULL DEFAULT 0,
                              ai_len INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE lore(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                          keys TEXT, content TEXT NOT NULL,
                          always INTEGER NOT NULL DEFAULT 0,
                          prio INTEGER NOT NULL DEFAULT 100,
                          enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE access(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                            username TEXT, added INTEGER NOT NULL);
        CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT,
                           first_name TEXT, seen INTEGER NOT NULL);
        CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    con.execute("INSERT INTO chats VALUES (?,?,?,?,1,0)", (CID, "Овощехранилище", None, 424211817))
    # осознанные настройки живого чата: миграция не вправе их трогать
    con.execute("""INSERT INTO settings (chat_id, ai_on, ai_persona, ai_random, ai_ctx,
                                         ai_daily, ai_names, ai_free, ai_len)
                   VALUES (?,1,'ехидный торговец',10,80,500,'холо',1,2)""", (CID,))
    con.execute("INSERT INTO kv VALUES ('mig_ctx50','1')")
    con.commit()
    con.close()

    con = sqlite3.connect(HIST)
    con.executescript("""
        CREATE TABLE ai_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                chat_id INTEGER NOT NULL, who TEXT NOT NULL,
                                text TEXT NOT NULL, ts INTEGER NOT NULL);
        CREATE INDEX idx_ai_history_chat ON ai_history(chat_id, id);
    """)
    for i in range(40):
        con.execute("INSERT INTO ai_history (chat_id, who, text, ts) VALUES (?,?,?,?)",
                    (CID, "@vasya", f"старая реплика {i}", 1700000000 + i))
    con.commit()
    con.close()


async def settle(done, tries=200):
    """Подождать фоновую задачу. Голого sleep(0) мало: aiosqlite ходит в базу
    в отдельном потоке, и ему нужно настоящее время, а не просто уступка."""
    for _ in range(tries):
        await asyncio.sleep(0.01)
        if done():
            return True
    return False


async def fake_model(system, question, tokens=0, images=None):
    ASKED.append((system, question, tokens))
    return "Вася — за пивом. Петя всегда пас. Шутка про овощехранилище."


async def main():
    build_old_bases()
    from slusha import ai, config, db, history as store, summary   # noqa: E402

    # --- 1. миграция основной базы на копии боевой ---
    await db.init()
    cols = await db.columns("settings")
    check("новые колонки настроек дописаны",
          {"ai_lang", "ai_vision", "ai_topics", "ai_greeting"} <= cols)
    s = await db.get_settings(CID)
    check("старые значения уцелели",
          (s.ai_random, s.ai_ctx, s.ai_daily, s.ai_len, s.ai_free) == (10, 80, 500, 2, 1))
    check("характер на месте", s.ai_persona == "ехидный торговец")
    check("у новых полей значения по умолчанию",
          (s.ai_lang, s.ai_vision, s.ai_topics) == (1, 0, 0))
    await db._migrate()                       # повторный прогон ничего не ломает
    check("миграция идемпотентна", (await db.get_settings(CID)).ai_ctx == 80)

    # --- 2. миграция базы переписки ---
    rows = await store.tail(CID, 100)
    check("старая переписка читается", len(rows) == 40)
    check("у старых строк msg_id пуст", rows[0].msg_id is None)
    check("и реакции пусты", rows[0].reactions == "")
    con = sqlite3.connect(HIST)
    cols = {r[1] for r in con.execute("PRAGMA table_info(ai_history)")}
    idx = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ai_history'")}
    con.close()
    check("колонки переписки дописаны",
          {"msg_id", "reply_to_id", "thread_id", "reactions"} <= cols)
    check("индекс по новой колонке создан после ALTER",
          "idx_ai_history_thread" in idx and "idx_ai_history_msg" in idx)

    await store.add(CID, "@petya", "новая реплика", 777, None, 42)
    fresh = (await store.tail(CID, 1))[0]
    check("новые поля пишутся", (fresh.msg_id, fresh.thread_id) == (777, 42))

    # --- 3. заметки собираются сами ---
    ai._ask_ollama = fake_model
    await ai.forget(CID)                      # начинаем с чистого листа
    for i in range(config.AI_SUMMARY_EVERY):
        await ai.remember(CID, "@vasya", f"реплика {i}", 1000 + i)
    await settle(lambda: bool(ASKED))
    await settle(lambda: True, 5)             # даём дописать результат в базу
    text, covered = await store.summary_get(CID)
    check("заметки собрались сами", bool(text))
    check("covered_id дошёл до последней реплики", covered > 0)
    check("модель просили именно пересказать", "заметки" in ASKED[-1][0].lower())
    check("в пересказ уехали реплики чата", "реплика 3" in ASKED[-1][1])

    block = await summary.block(CID)
    check("заметки уходят в промпт", "Вася — за пивом" in block)
    check("и помечены как справка", "не инструкции" in block)

    # --- 4. второй раз пересказываем только новое ---
    ASKED.clear()
    for i in range(config.AI_SUMMARY_EVERY):
        await ai.remember(CID, "@petya", f"свежак {i}", 2000 + i)
    check("вторая пересборка случилась", await settle(lambda: bool(ASKED)))
    check("прошлые заметки показали модели", "Вася — за пивом" in ASKED[-1][1])
    check("старое второй раз не пересказываем", "реплика 3" not in ASKED[-1][1])
    check("а новое — пересказываем", "свежак 3" in ASKED[-1][1])

    # --- 5. флаг «уже сжимаю» ---
    summary._busy.add(CID)
    ASKED.clear()
    for i in range(config.AI_SUMMARY_EVERY * 2):
        await ai.remember(CID, "@kolya", f"пока занято {i}", 3000 + i)
    await settle(lambda: bool(ASKED), 30)
    check("пока идёт пересборка, вторую не запускаем", not ASKED)
    summary._busy.discard(CID)

    # --- 6. слишком длинные заметки режутся ---
    async def verbose(system, question, tokens=0, images=None):
        return "очень подробно. " * 1000

    ai._ask_ollama = verbose
    await summary._compact(CID)
    text, _ = await store.summary_get(CID)
    check("заметки обрезаны до потолка", 0 < len(text) <= config.AI_SUMMARY_LIMIT)

    # --- 7. «забыть переписку» стирает и заметки ---
    await ai.forget(CID)
    check("заметок не осталось", (await store.summary_get(CID))[0] == "")
    check("и блок в промпте пуст", await summary.block(CID) == "")

    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
