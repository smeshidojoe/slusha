"""Прогон Слюши без Telegram: база, меню, карточка персонажа, промпт."""
import asyncio
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="slusha-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "s.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "s.log"))
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db, history, lore, menu, schema   # noqa: E402

OWNER = 424211817
CID = -1001234567890
# карточка персонажа для проверки импорта: путь задаётся снаружи,
# без него эти проверки просто пропускаются
CARD = os.getenv("SLUSHA_TEST_CARD", "")
FAILS = []


class FakeUser:
    username = "slusha_bot"
    full_name = "Слюша"
    id = 1000


class FakeBot:
    async def me(self):
        return FakeUser()


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


async def main():
    await db.init()
    await db.upsert_chat(CID, "Овощехранилище", None, OWNER)

    s = await db.get_settings(CID)
    check("настройки заводятся", s.ai_len == 1 and s.ai_on == 0)

    # поля схемы крутятся
    f = schema.BY_KEY["ai_random"]
    nxt = schema.cycle(f, s.ai_random, 1)
    await db.set_setting(CID, "ai_random", nxt)
    check("селектор шанса", (await db.get_settings(CID)).ai_random == nxt)
    await db.set_setting(CID, "ai_on", 1)

    # экраны меню собираются
    bot = FakeBot()
    for name, coro in (("home", menu.view_home(OWNER)),
                       ("chats", menu.view_chats(bot, OWNER)),
                       ("chat", menu.view_chat(CID)),
                       ("lore", menu.view_lore(CID)),
                       ("access", menu.view_access())):
        text, kb = await coro
        check(f"экран {name}", bool(text) and kb is not None)

    # карточка персонажа
    if os.path.exists(CARD):
        raw = open(CARD, "rb").read()
        result = await lore.import_file(CID, raw)
        card = result["card"]
        done = await lore.apply_card(CID, card)
        s = await db.get_settings(CID)
        check("характер из карточки", len(s.ai_persona or "") > 3000)
        check("имя в обращениях", "belisarius" in (s.ai_names or ""))
        check("плейсхолдеры подставлены", "{{" not in (s.ai_persona or ""))
        print("     применили:", done, "| знаков:", len(s.ai_persona))

    # промпт с правилом языка
    s = await db.get_settings(CID)
    prompt = ai._prompt(s, "Овощехранилище", "@mike", ["@slusha_bot", "Слюша"])
    check("в промпте правило про русский", "по-русски" in prompt)
    check("в промпте характер", "Cawl" in prompt or "Слюша" in prompt)

    # лор подмешивается даже без совпадения ключей
    await db.lore_add(CID, "mars", "Марс — кузница Механикус." * 20)
    block = await lore.block(CID, "привет как дела")
    check("фоновый лор подмешался", bool(block))
    check("лор влез в бюджет", len(block) <= config.LORE_BUDGET + 200)

    # лимит ответов в сутки
    spent = await ai.spent_today(CID)
    await ai._count(CID)
    check("счётчик ответов", await ai.spent_today(CID) == spent + 1)

    # экран чата показывает заметки, то есть открывает базу переписки:
    # её соединение держит свой поток, и без close процесс не завершится
    await history.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
