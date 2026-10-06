"""Долгая память (memory.py) без модели: эмбеддер и gemma подменены."""
import asyncio
import hashlib
import os
import re
import sys
import tempfile
import time

import numpy as np

TMP = tempfile.mkdtemp(prefix="slusha-deepmem-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="1",
                  SLUSHA_DB_PATH=os.path.join(TMP, "s.sqlite3"),
                  SLUSHA_HISTORY_DB=os.path.join(TMP, "h.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "s.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://ollama.invalid", AI_MODEL="gemma3:4b",
                  MEM_VAULT=os.path.join(TMP, "vault"))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import importlib                                        # noqa: E402
_PKG = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("slusha", importlib.import_module(_PKG))

from slusha import ai, db, history, memory   # noqa: E402
Line = history.Line                          # тот же модуль: в форке «slusha.history» — второй экземпляр

CID = -1010
FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


async def fake_embed(texts):
    """Мешок корней по 4 буквы в 512 измерениях: похожие фразы — близкие векторы."""
    out = []
    for t in texts:
        v = np.zeros(512, dtype=np.float32)
        for w in re.findall(r"\w{3,}", t.lower()):
            v[int(hashlib.md5(w[:4].encode()).hexdigest(), 16) % 512] += 1
        v[511] += 0.3                     # общий фон: у всех фраз немного общего
        out.append(v / np.linalg.norm(v))
    return out


ANSWERS = {}


async def fake_raw(system, question, tokens, images=None):
    if "Найди в переписке события" in question:
        return ANSWERS["journal"]
    if "Подтверждает ли эта реплика" in question:
        return "нет" if "торт" in question else "да"
    if "Сделай один вывод" in question:
        return ANSWERS.get("reflect", "Сейчас для @telgii важны рыбки и аквариумы.")
    return "ответ"


async def main():
    await db.init()
    ai.raw = fake_raw
    memory.embed = fake_embed
    now = int(time.time())

    # --- журнал ---
    rows = [Line("@telgii", "я спасла рыбку из магазина, построила ей аквариум", ts=now - 3600),
            Line("@telgii", "кормлю её каждый день, плавник отрос", ts=now - 3500),
            Line("@vasya", "а я купил велосипед наконец", ts=now - 3400),
            Line("@petya", "Вася, круто", ts=now - 3300),
            Line("ты", "Рыбки — неэффективные существа.", ts=now - 3200),
            Line("@masha", "мама испекла торт", ts=now - 3100)]
    ANSWERS["journal"] = (
        "Вот события:\n"
        "@telgii — спасла рыбку из магазина и построила ей аквариум | 8\n"
        "@vasya — купил велосипед | 5\n"
        "@petya — сломал ногу на лыжах | 7\n"         # опоры в его репликах нет
        "@vasya — спросил погоду | 2\n"                # мелочь
        "@nobody — улетел на Марс | 9\n"               # его нет в переписке
        "@vasya — назвал Коула роботом | 6\n"          # о самом боте
        "@masha — испекла торт | 5")                   # модель не подтвердила
    n = await memory.journal(CID, rows, ["коул", "белизарий"])
    con = await history._conn()
    cur = await con.execute("SELECT who, text FROM ai_events WHERE chat_id = ? ORDER BY id", (CID,))
    got = [(r["who"], r["text"]) for r in await cur.fetchall()]
    check(f"в журнал двое: {got}", n == 2 and [w for w, _ in got] == ["@telgii", "@vasya"])

    ANSWERS["journal"] = "@telgii — спасла рыбку из магазина и построила аквариум | 8"
    check("повтор не пишем", await memory.journal(CID, rows, ["коул"]) == 0)

    # --- отбор ---
    pool = ["участвует в обсуждениях", "любит шутить", "играет в доту", "смотрит аниме",
            "рисует арты", "болеет дома"]
    line = await memory.remembered(CID, "у меня рыбка в аквариуме плавником не шевелит", pool)
    check(f"рыбку вспомнили: {line}", line.startswith("ты помнишь: сегодня @telgii") and "рыбку" in line)
    check("а велосипед — нет", "велосипед" not in line)
    check("о постороннем — ничего", await memory.remembered(CID, "кто будет пиццу вечером?", pool) == "")
    cur = await con.execute("SELECT hits FROM ai_events WHERE who = '@telgii'")
    check("вспоминание посчитано", (await cur.fetchone())["hits"] == 1)

    # --- в ответе: воспоминание приписано к самому вопросу ---
    sent = []

    async def answering(system, question, tokens, images=None):
        if isinstance(question, list):           # сам ответ — ходы переписки
            sent.append(question)
            return "Опять рыбка?"
        return await fake_raw(system, question, tokens, images)

    ai.raw = answering
    await db.upsert_chat(CID, "Чат", None, 1)
    s = await db.get_settings(CID)
    q = "у меня рыбка в аквариуме плавником не шевелит"
    await ai.ask(s, "Чат", CID, "@telgii", q, ["Коул"], snapshot=[Line("@telgii", q, 900)])
    flat = str(sent[0])
    check("воспоминание в вопросе", f"{q} [ты помнишь: сегодня @telgii — спасла рыбку" in flat)
    await db.set_setting(CID, "ai_journal", 0)
    sent.clear()
    await ai.ask(await db.get_settings(CID), "Чат", CID, "@telgii", q, ["Коул"],
                 snapshot=[Line("@telgii", q, 900)])
    check("дневник выключен — без воспоминаний", "ты помнишь" not in str(sent[0]))
    await db.set_setting(CID, "ai_journal", 1)
    ai.raw = fake_raw

    # --- уборка ---
    old = now - 40 * 86400
    await con.executemany(
        "INSERT INTO ai_events (chat_id, key, who, text, importance, ts, hits) VALUES (?,?,?,?,?,?,?)",
        [(CID, "@vasya", "@vasya", "сходил в кино на мультик", 4, old, 0),      # забыть
         (CID, "@vasya", "@vasya", "женился", 9, old, 0),                       # оставить
         (CID, "@vasya", "@vasya", "Купил велосипед!", 6, now - 60, 0),  # повтор
         (CID, "@telgii", "@telgii", "завела вторую рыбку", 6, now - 50, 0),
         (CID, "@telgii", "@telgii", "купила корм для рыбок", 5, now - 40, 0)])
    await con.commit()
    stat = await memory.tidy(CID)
    cur = await con.execute("SELECT who, text, kind FROM ai_events WHERE chat_id = ? ORDER BY id", (CID,))
    left = [(r["who"], r["text"], r["kind"]) for r in await cur.fetchall()]
    texts = [t for _, t, _ in left]
    check(f"старая мелочь забыта: {stat}", "сходил в кино на мультик" not in texts)
    check("важное живёт", "женился" in texts)
    check("повтор склеен", stat["merged"] == 1 and texts.count("купил велосипед") + texts.count(
        "Купил велосипед!") == 1)
    check(f"вывод о человеке: {left[-1]}", left[-1][2] == "insight" and left[-1][0] == "@telgii")

    # --- выгрузка в Obsidian ---
    await history.people_set(CID, [("@telgii", "держит рыбок; дружит с @vasya")])
    root = await memory.export(CID, "Чат: тест/1")
    person = open(os.path.join(root, "Люди", "@telgii.md"), encoding="utf-8").read()
    check("папка чата с безопасным именем", os.path.basename(root) == "Чат_ тест_1")
    check("заметка о человеке", "спасла рыбку" in person and "## Выводы" in person)
    check("ники стали ссылками", "[[@vasya]]" in person)
    days = os.listdir(os.path.join(root, "Дни"))
    check(f"дни: {days}", any(d.startswith(time.strftime("%Y-%m-%d")) for d in days))
    check("главная", os.path.exists(os.path.join(root, "Главная.md")))

    # --- договорённости и шутки из заметок — в долгой памяти ---
    from slusha import summary
    general = ("ДОГОВОРЁННОСТИ И СОБЫТИЯ:\n"
               "— договорились в субботу сыграть в доту впятером\n"
               "— @vasya заказал пиццу на всех\n"
               "ШУТКИ И ПРОЗВИЩА:\n"
               "— «сыр колбаса» — присказка @masha\n"
               "ФАКТЫ О ТЕБЕ:\n— бота зовут роботом\n"
               "СЕЙЧАС ОБСУЖДАЮТ:\n— погоду")
    rest, items = summary._lift(general)
    check(f"разделы вынуты: {items}", len(items) == 3 and "доту" not in rest)
    check("заголовки остались с прочерком",
          "ДОГОВОРЁННОСТИ И СОБЫТИЯ:\n—\nШУТКИ И ПРОЗВИЩА:\n—\nФАКТЫ О ТЕБЕ:" in rest)
    check("вынуть второй раз — нечего", summary._lift(rest) == (rest, []))
    check("в память легли три", await memory.keep_notes(CID, items, now - 100) == 3)
    check("повтор другими словами не пишется", await memory.keep_notes(
        CID, [("deal", "— Договорились: в субботу сыграть в доту впятером!")], now) == 0)
    cur = await con.execute("SELECT ts FROM ai_events WHERE kind = 'deal' AND text LIKE '%доту%'")
    check("а дата у старой освежилась", (await cur.fetchone())["ts"] == now)
    cur = await con.execute("SELECT who, key FROM ai_events WHERE kind = 'joke'")
    r = await cur.fetchone()
    check("шутка привязана к нику", (r["who"], r["key"]) == ("@masha", "@masha"))

    await history.summary_set(CID, rest, 0)
    got = await summary.block(CID, [], "ну что, в субботу дота в силе?")
    check(f"к разговору — подходящая договорённость: {got}",
          "сыграть в доту" in got and "пиццу" not in got and "ШУТКИ" not in got)
    check("общие разделы на месте", "ФАКТЫ О ТЕБЕ" in got and "погоду" in got)
    got = await summary.block(CID, [], "кто-нибудь видел мои ключи от машины")
    check(f"к постороннему — ни одной: {got}", "ДОГОВОРЁННОСТИ" not in got and "ШУТКИ" not in got)

    # заметки, записанные раньше, переезжают в память при первом чтении
    await con.execute("DELETE FROM ai_events WHERE kind IN ('deal', 'joke')")
    await history.summary_set(CID, general, 0)
    left = await summary._general(CID)
    check("старые заметки разложены", left == rest and len(await memory.notes_all(CID)) == 3)

    # то, что не вспоминалось месяц, забывается ночью
    await con.execute("UPDATE ai_events SET ts = ? WHERE kind = 'joke'", (now - 40 * 86400,))
    await con.commit()
    await memory.tidy(CID)
    check("старая шутка забыта", [r["kind"] for r in await memory.notes_all(CID)] == ["deal", "deal"])
    config_notes = memory.config.MEM_NOTES
    memory.config.MEM_NOTES = 0
    got = await summary.block(CID, [], "в субботу дота в силе?")
    check(f"выключено — свежие из памяти, как раньше: {got}",
          "ДОГОВОРЁННОСТИ И СОБЫТИЯ:\n— " in got and "пиццу" in got and "\n—\n" not in got)
    memory.config.MEM_NOTES = config_notes

    # --- забыть человека — и его события, и где он назван ---
    await history.people_forget(CID, ["@vasya"])
    cur = await con.execute("SELECT text FROM ai_events WHERE chat_id = ?", (CID,))
    rest = [r["text"] for r in await cur.fetchall()]
    check(f"забыт вместе с событиями: {rest}", not any("велосипед" in t or "женился" in t for t in rest))
    await history.summary_clear(CID)
    cur = await con.execute("SELECT COUNT(*) AS n FROM ai_events WHERE chat_id = ?", (CID,))
    check("стёрли память — стёрли и дневник", (await cur.fetchone())["n"] == 0)

    await history.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
