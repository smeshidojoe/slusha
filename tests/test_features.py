"""Слюша: имена по границам слов, шум, лимит токенов, картинки, ветки, темы.

Тут проверяются те грабли, из-за которых бот вёл себя навязчиво или невпопад:
«Холо» внутри «холодно», ответы на «ок», обрыв длинного ответа на полуслове,
мысли модели в чате, ответ мимо темы форума.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-feat-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db, reactions          # noqa: E402
from slusha.history import Line                        # noqa: E402

CID = -100999
FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def me(self):
        return SimpleNamespace(id=1000, username="slusha_bot", full_name="Слюша")

    async def send_chat_action(self, *a, **kw):
        return True

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=500 + len(self.sent))


def msg(text, uid=7, mid=1, reply=None, topic=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=CID, title="Чат", type="supergroup"),
        from_user=SimpleNamespace(id=uid, username="vasya", full_name="Вася",
                                  is_bot=False),
        text=text, caption=None, message_id=mid, reply_to_message=reply,
        is_topic_message=topic is not None, message_thread_id=topic)


class FakeResp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """Подставной httpx: запоминает тело запроса и отвечает как настоящий."""

    def __init__(self):
        self.calls = []

    async def post(self, path, json=None):
        self.calls.append((path, json))
        if path == "/api/chat":
            return FakeResp({"message": {"content": "ага"}})
        return FakeResp({"choices": [{"message": {"content": "ага"}}]})


async def main():
    await db.init()
    await db.upsert_chat(CID, "Чат", None, 424211817)
    await db.set_setting(CID, "ai_on", 1)
    bot = FakeBot()
    real_ollama = ai._ask_ollama

    # --- A1: имя срабатывает только целым словом ---
    # Без звёздочки имя ловится строго целиком: ровно та беда, из-за которой
    # бот по имени «Холо» лез в разговор про холодильник.
    await db.set_setting(CID, "ai_names", "холо, слюша")
    s = await db.get_settings(CID)
    cases = [("холодно на улице", False), ("холодильник гудит", False),
             ("холопы бунтуют", False), ("нехолодно вроде", False),
             ("холо, привет", True), ("ну ты и холо", True),
             ("слюша тут?", True), ("слюшать это невозможно", False)]
    for text, want in cases:
        got = await ai.addressed(bot, msg(text), s)
        check(f"имя «холо» в «{text}» -> {want}", got == want)

    # Со звёздочкой ловятся падежи. Плата за это — однокоренные слова тоже
    # сработают: звёздочку ставят осознанно, когда имя редкое.
    await db.set_setting(CID, "ai_names", "холо*")
    s = await db.get_settings(CID)
    for text, want in [("спроси у холой", True), ("холочка миленькая", True),
                       ("нехолодно вроде", False), ("прохолодился", False)]:
        got = await ai.addressed(bot, msg(text), s)
        check(f"имя «холо*» в «{text}» -> {want}", got == want)
    await db.set_setting(CID, "ai_names", "холо, слюша")
    s = await db.get_settings(CID)
    check("юзернейм по-прежнему ловится",
          await ai.addressed(bot, msg("эй @slusha_bot"), s))
    check("без имён выражения нет", ai.name_re(SimpleNamespace(ai_names="")) is None)

    # --- A2: шум ---
    for text in ("ок", "ага", "+", "++", "лол", "хахаха", "👍", "да ну", "спс", "..."):
        check(f"«{text}» — шум", ai.noisy(text))
    for text in ("пойдём в кино вечером", "а что там с сервером", "кто последний"):
        check(f"«{text}» — не шум", not ai.noisy(text))

    await db.set_setting(CID, "ai_random", 50)
    await db.set_setting(CID, "ai_names", "")
    s = await db.get_settings(CID)
    real = ai.random.randrange
    ai.random.randrange = lambda n: n // 4          # ровно четверть диапазона
    try:
        check("на осмысленной реплике шанс срабатывает",
              await ai.should_reply(bot, msg("кто пойдёт за пивом сегодня"), s))
        check("на шуме тот же бросок уже не проходит",
              not await ai.should_reply(bot, msg("ок"), s))
    finally:
        ai.random.randrange = real
    await db.set_setting(CID, "ai_random", 100)
    s = await db.get_settings(CID)
    check("прямое обращение отвечается и на шуме",
          await ai.should_reply(bot, msg("@slusha_bot ок"), s))

    # --- A3: лимит токенов зависит от длины ответа ---
    lens = {}
    for value in (0, 1, 2):
        await db.set_setting(CID, "ai_len", value)
        lens[value] = ai.max_tokens(await db.get_settings(CID))
    check("«коротко» дешевле «средне»", lens[0] < lens[1])
    check("«развёрнуто» дороже «средне»", lens[2] > lens[1])
    check("развёрнутому хватает на 1800 знаков",
          lens[2] >= config.AI_LEN_RULES[2][1] / 3)

    # --- A4: запас на размышления там, где их не выключить ---
    await db.set_setting(CID, "ai_len", 1)
    s = await db.get_settings(CID)
    plain = ai.max_tokens(s)
    was = config.AI_PROVIDER
    config.AI_PROVIDER = "anthropic"
    try:
        check("у думающего провайдера лимит с запасом",
              ai.max_tokens(s) == plain + config.AI_THINKING_RESERVE)
    finally:
        config.AI_PROVIDER = was
    check("у обычной модели запаса нет", ai.max_tokens(s) == plain)

    # --- A4: мысли вырезаются во всех видах ---
    check("<think> закрытый", ai.strip_thoughts("<think>ммм</think>ответ") == "ответ")
    check("<thinking> закрытый",
          ai.strip_thoughts("<thinking>ммм</thinking>ответ") == "ответ")
    check("незакрытый в конце",
          ai.strip_thoughts("ответ<think>а что если").strip() == "ответ")
    check("закрывающий без открывающего",
          ai.strip_thoughts("долго думаю</think>ответ") == "ответ")
    check("обычный текст не трогаем", ai.strip_thoughts("просто ответ") == "просто ответ")

    # длинный ответ с мыслями: раньше он целиком считался размышлением и терялся
    async def thoughtful(system, question, tokens=0, images=None):
        return "<think>" + "рассуждаю. " * 200 + "</think>Короткий ответ."

    ai._ask_ollama = thoughtful
    parts = await ai.ask(s, "Чат", CID, "@vasya", "как дела?")
    check("ответ из-под простыни мыслей уцелел", parts == ["Короткий ответ."])

    # --- A6: ответ остаётся в своей теме форума ---
    check("тема сообщения распознана", ai.thread_of(msg("привет", topic=77)) == 77)
    check("в обычной группе темы нет", ai.thread_of(msg("привет")) is None)

    async def two_parts(system, question, tokens=0, images=None):
        return "первая часть\n\nвторая часть"

    ai._ask_ollama = two_parts
    bot.sent.clear()
    await ai._respond(bot, msg("вопрос", mid=11, topic=77), s, "@vasya", "вопрос",
                      snapshot=[], thread=77)
    check("обе части ушли", len(bot.sent) == 2)
    check("тема указана у всех частей",
          all(kw.get("message_thread_id") == 77 for _, _, kw in bot.sent))
    check("реплай только у первой части",
          bot.sent[0][2].get("reply_to_message_id") == 11
          and bot.sent[1][2].get("reply_to_message_id") is None)

    bot.sent.clear()
    await ai._respond(bot, msg("вопрос", mid=12), s, "@vasya", "вопрос", snapshot=[])
    check("в обычной группе темы не появляется",
          all("message_thread_id" not in kw for _, _, kw in bot.sent))

    # --- B2: картинки в каждом формате ---
    blocks = ai._anthropic_content("вопрос", ["QkFTRTY0"])
    check("anthropic: картинка блоком base64",
          blocks[0]["type"] == "image"
          and blocks[0]["source"]["media_type"] == "image/jpeg"
          and blocks[0]["source"]["data"] == "QkFTRTY0"
          and blocks[-1]["text"] == "вопрос")
    check("anthropic без картинок — просто текст",
          ai._anthropic_content("вопрос", None) == "вопрос")

    # выше _ask_ollama подменяли на заглушки — возвращаем настоящий,
    # иначе тело запроса собирать некому
    ai._ask_ollama = real_ollama
    http = FakeHttp()
    ai._client = http
    await ai._ask_ollama("sys", "вопрос", 700, ["QkFTRTY0"])
    body = http.calls[-1][1]
    check("ollama: картинки отдельным полем сообщения",
          body["messages"][-1]["images"] == ["QkFTRTY0"])
    check("ollama: лимит токенов из настройки", body["options"]["num_predict"] == 700)

    await ai._ask_openai("sys", "вопрос", 700, ["QkFTRTY0"])
    body = http.calls[-1][1]
    content = body["messages"][-1]["content"]
    check("openai: картинка как data-url",
          content[0]["type"] == "image_url"
          and content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
    check("openai: текст следом за картинкой", content[-1]["text"] == "вопрос")
    check("openai: лимит токенов из настройки", body["max_tokens"] == 700)
    await ai._ask_openai("sys", "вопрос", 700)
    check("без картинок content остаётся строкой",
          http.calls[-1][1]["messages"][-1]["content"] == "вопрос")
    ai._client = None

    # --- B3: ветка реплаев ---
    rows = [
        Line("@vasya", "первое", 1, None),
        Line("@petya", "чужое", 2, None),
        Line(ai.SELF, "мой ответ", 3, 1),
        Line("@kolya", "ответ боту", 4, 3),
        Line("@kolya", "и ещё", 5, 4),
    ]
    branch = ai.chain(rows, 5)
    check("ветка собрана сверху вниз",
          [line.text for line in branch] == ["первое", "мой ответ",
                                             "ответ боту", "и ещё"])
    check("чужая реплика в ветку не попала",
          all(line.text != "чужое" for line in branch))
    check("глубина ограничена", len(ai.chain(rows, 5)) <= config.REPLY_CHAIN_DEPTH)
    check("без реплая ветки нет", ai.chain(rows, None) == [])
    check("оборванная ссылка не роняет", ai.chain(rows, 999) == [])
    loop_rows = [Line("@vasya", "сам себе", 9, 9)]
    check("цикл не зациклил", len(ai.chain(loop_rows, 9)) == 1)

    # --- B4: реакции ---
    def r(kind, emoji=None):
        return SimpleNamespace(type=kind, emoji=emoji)

    change = reactions.delta([], [r("emoji", "👍")])
    check("новая реакция — плюс один", change == {"👍": 1})
    check("снятая — минус один",
          reactions.delta([r("emoji", "👍")], []) == {"👍": -1})
    check("неизменившаяся не считается",
          reactions.delta([r("emoji", "👍")], [r("emoji", "👍"), r("emoji", "🔥")])
          == {"🔥": 1})
    check("кастомную помечаем значком",
          reactions.delta([], [r("custom_emoji")]) == {reactions.CUSTOM: 1})
    line = reactions.merge(reactions.merge("", {"👍": 1}), {"👍": 1, "🔥": 1})
    check("счётчик рендерится", line == "👍×2, 🔥")
    check("строка читается обратно", reactions.parse(line) == {"👍": 2, "🔥": 1})
    check("до нуля — реакция исчезает",
          reactions.merge("👍×2, 🔥", {"🔥": -1}) == "👍×2")

    await ai.remember(CID, "@vasya", "смешная шутка", 42)
    await ai.set_reactions(CID, 42, "👍×2")
    got = [ln for ln in await ai.history(CID, 50) if ln.msg_id == 42]
    check("реакции легли в память", got and got[0].reactions == "👍×2")
    rendered = ai._render(got)
    check("и рендерятся в промпте", "[реакции: 👍×2]" in rendered[0])

    # --- B5: изоляция по темам ---
    await ai.forget(CID)
    await ai.remember(CID, "@vasya", "про баню", 101, None, 77)
    await ai.remember(CID, "@petya", "про машину", 102, None, 88)
    await ai.remember(CID, "@kolya", "и про веники", 103, None, 77)
    banya = [ln.text for ln in await ai.history(CID, 50, 77)]
    check("в тему попали только её реплики", banya == ["про баню", "и про веники"])
    check("без фильтра видно всё", len(await ai.history(CID, 50)) == 3)

    from slusha import history as store
    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
