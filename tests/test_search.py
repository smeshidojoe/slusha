"""Поиск в интернете без сети: SearXNG, скачивание и модель подменены."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-search-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="1",
                  SLUSHA_DB_PATH=os.path.join(TMP, "s.sqlite3"),
                  SLUSHA_HISTORY_DB=os.path.join(TMP, "h.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "s.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://ollama.invalid", AI_MODEL="gemma3:4b",
                  SEARCH_URL="http://searx.invalid", SEARCH_DIR=os.path.join(TMP, "search"),
                  SEARCH_SELF="Belisarius Cawl art")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, db, history, search   # noqa: E402

CID = -1009
FAILS = []
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
ME = SimpleNamespace(id=1000, username="bot", full_name="Белизарий Коул", is_bot=True)


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class Bot:
    def __init__(self):
        self.sent = []

    async def me(self):
        return ME

    async def send_chat_action(self, *a, **k):
        pass

    async def send_message(self, chat_id, text, **k):
        self.sent.append(("text", text))
        return SimpleNamespace(message_id=9001)

    async def send_photo(self, chat_id, photo, caption=None, **k):
        self.sent.append(("photo", photo.path, caption))
        return SimpleNamespace(message_id=9002)


def msg(text, mid, reply=None):
    return SimpleNamespace(chat=SimpleNamespace(id=CID, title="Чат", type="supergroup"),
                           from_user=SimpleNamespace(id=7, username="vasya", full_name="Вася",
                                                     is_bot=False),
                           text=text, caption=None, message_id=mid, reply_to_message=reply,
                           sticker=None, photo=None)


async def main():
    await db.init()
    s = await db.get_settings(CID)
    check("настройка поиска заводится включённой", s.ai_search == 1)

    # --- фильтр по словам и шаблон своего фото — без модели ---
    check("«скинь фото» проходит фильтр", search.wanted("Коул, скинь фото с пивом"))
    check("обычная реплика — нет", not search.wanted("Коул, как дела?"))
    check("своё фото — по шаблону", await search.intent("коул, а покажи свою фотку")
          == ("self", "Belisarius Cawl art"))

    # --- разбор ответа модели ---
    answers = {}

    async def fake_raw(system, question, tokens, images=None):
        if images:
            return answers["look"](question)
        if "Определи, нужен ли поиск" in question:
            return answers["what"]
        if "Ответь на вопрос по выдержкам" in question:
            return "Красноухие черепахи живут 30–40 лет."
        return "ответ"

    real_raw = ai.raw
    ai.raw = fake_raw
    answers["what"] = "КАРТИНКА: бутылка пива"
    check("картинка", await search.intent("Коул, скинь фото с бутылкой пива")
          == ("img", "бутылка пива"))
    answers["what"] = "ИНФО: где тут желе"
    check("справка без слова «поищи» — нет", await search.intent("покажи, где тут желе")
          == ("", ""))
    answers["what"] = "ИНФО: сколько живут черепахи"
    check("справка со словом «погугли»", await search.intent("коул погугли сколько живут черепахи")
          == ("info", "сколько живут черепахи"))
    answers["what"] = "ИНФО: запрос"
    check("выдуманный запрос не ищем", await search.intent("коул найди в интернете, что такое тардиград")
          == ("", ""))
    answers["what"] = "КАРТИНКА: его (место, где есть еды)"
    check("«его» — искать нечего", await search.intent("Покажи мне его") == ("", ""))
    answers["what"] = "НЕТ"
    check("метафора — без поиска", await search.intent("Хорошо, позволяю. Покажи") == ("", ""))

    # --- картинка: первая не та, вторая неприличная, третья годится ---
    async def fake_searx(query, category):
        if category == "images":
            return [{"img_src": f"http://img/{i}.png"} for i in range(5)]
        return [{"title": "Черепахи", "content": "Красноухая — 30–40 лет."}]

    async def fake_download(client, url):
        return PNG + url.encode()

    def look(question):
        n = search._last_url[-5]
        if "Есть ли на этой картинке бутылка" in question:
            return "нет" if n == "0" else "да"
        if "непристойное" in question:
            return "да" if n == "1" else "нет"
        return "Бутылка пива на столе"

    real_searx, real_download = search._searx, search._download
    search._last_url = ""

    async def tracking_download(client, url):
        search._last_url = url
        return await fake_download(client, url)

    search._searx, search._download = fake_searx, tracking_download
    answers["look"] = look
    got = await search.picture(CID, "бутылка пива")
    check(f"взяли третью: {got}", got is not None and got[1] == "Бутылка пива на столе")
    check("картинка легла в папку search", got is not None and os.path.dirname(got[0])
          == os.environ["SEARCH_DIR"] and os.path.exists(got[0]))

    # --- целиком: просьба — фото с подписью ---
    answers["what"] = "КАРТИНКА: бутылка пива"
    bot = Bot()
    m = msg("Коул, скинь фото с бутылкой пива", 501)
    await ai.remember(CID, "@vasya", m.text, 501, None, None)
    snap = await ai.history(CID, s.ai_ctx)
    await ai._respond(bot, m, s, "@vasya", m.text, snap, None, None, [], None, False)
    check(f"ушло фото с подписью: {bot.sent}", bot.sent and bot.sent[0][0] == "photo"
          and bot.sent[0][2] == "ответ")
    rows = [r.text for r in await ai.history(CID, 5)]
    check(f"в истории — фото словами: {rows[-1]}", rows[-1] == "[фото: Бутылка пива на столе] ответ")

    # --- справка: в сам вопрос, в историю не пишем ---
    asked = []

    async def fake_ask(s_, title, cid, who, question, names, snapshot=None, **k):
        asked.append((question, [ln.text for ln in snapshot]))
        return ["ответ про черепах"]

    real_ask = ai.ask
    ai.ask = fake_ask
    answers["what"] = "ИНФО: сколько живут черепахи"
    bot = Bot()
    m = msg("коул погугли сколько живут черепахи", 502)
    await ai.remember(CID, "@vasya", m.text, 502, None, None)
    snap = await ai.history(CID, s.ai_ctx)
    await ai._respond(bot, m, s, "@vasya", m.text, snap, None, None, [], None, False)
    want = "коул погугли сколько живут черепахи [нашёл в интернете: Красноухие черепахи живут 30–40 лет.]"
    check("справка в вопросе", asked and asked[-1][0] == want)
    check("и в переписке один раз", asked and asked[-1][1][-1] == want
          and asked[-1][1].count(want) == 1)
    rows = [r.text for r in await ai.history(CID, 5)]
    check("в историю — без справки", "коул погугли сколько живут черепахи" in rows)

    # --- выключатель и лимит ---
    await db.set_setting(CID, "ai_search", 0)
    s_off = await db.get_settings(CID)
    check("выключено — не ищем", not search.enabled(s_off))
    search._spent[CID] = (search.datetime.date.today().isoformat(), search.config.SEARCH_DAILY)
    check("лимит на сутки", await search.picture(CID, "бутылка пива") is None)

    ai.raw, ai.ask = real_raw, real_ask
    search._searx, search._download = real_searx, real_download
    await history.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
