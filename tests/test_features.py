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

    # --- шанс ответить на ответ себе ---
    # Раньше реплай боту отвечался всегда, и разговор вырождался в пинг-понг:
    # человек отвечает боту, бот обязательно человеку, тот снова боту.
    def from_bot(text="моя реплика"):
        return SimpleNamespace(from_user=SimpleNamespace(id=1000,
                                                         username="slusha_bot",
                                                         full_name="Слюша"),
                               text=text, caption=None, message_id=99)

    def from_human(text="чужая реплика"):
        return SimpleNamespace(from_user=SimpleNamespace(id=8, username="petya",
                                                         full_name="Петя"),
                               text=text, caption=None, message_id=98)

    await db.set_setting(CID, "ai_random", 0)
    await db.set_setting(CID, "ai_names", "слюша")
    await db.set_setting(CID, "ai_reply", 35)
    s = await db.get_settings(CID)
    check("значение по умолчанию — 35", s.ai_reply == 35)

    to_bot = msg("а почему так", mid=20, reply=from_bot())
    check("реплай боту распознан", await ai.replied_to(bot, to_bot))
    check("реплай другому — не нам",
          not await ai.replied_to(bot, msg("ага", mid=21, reply=from_human())))

    real = ai.random.randrange
    ai.random.randrange = lambda n: 10          # бросок ниже 35
    try:
        check("удачный бросок — отвечаем", await ai.should_reply(bot, to_bot, s))
    finally:
        ai.random.randrange = real
    ai.random.randrange = lambda n: 40          # бросок выше 35
    try:
        check("неудачный бросок — молчим", not await ai.should_reply(bot, to_bot, s))
        check("и на случайный шанс это не проваливается",
              not await ai.should_reply(bot, to_bot, s))
        by_name = msg("слюша, а почему так", mid=22, reply=from_bot())
        check("имя в том же сообщении отвечается всегда",
              await ai.should_reply(bot, by_name, s))
    finally:
        ai.random.randrange = real

    await db.set_setting(CID, "ai_reply", 0)
    s = await db.get_settings(CID)
    check("ноль — на реплаи не отвечаем вовсе",
          not any([await ai.should_reply(bot, to_bot, s) for _ in range(50)]))
    check("но по имени зовётся по-прежнему",
          await ai.should_reply(bot, msg("слюша ау", mid=23), s))

    await db.set_setting(CID, "ai_reply", 100)
    s = await db.get_settings(CID)
    check("сотня — отвечаем на каждый ответ себе",
          all([await ai.should_reply(bot, to_bot, s) for _ in range(50)]))

    # частота держится около выставленной
    await db.set_setting(CID, "ai_reply", 35)
    s = await db.get_settings(CID)
    hits = sum([await ai.should_reply(bot, to_bot, s) for _ in range(2000)])
    check(f"частота около 35% (вышло {hits * 100 // 2000}%)",
          500 < hits < 900)

    await db.set_setting(CID, "ai_names", "")
    await db.set_setting(CID, "ai_random", 100)

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
    one = [{"role": "user", "content": "вопрос"}]
    await ai._ask_ollama("sys", one, 700, ["QkFTRTY0"])
    body = http.calls[-1][1]
    check("ollama: картинки отдельным полем сообщения",
          body["messages"][-1]["images"] == ["QkFTRTY0"])
    check("ollama: лимит токенов из настройки", body["options"]["num_predict"] == 700)

    await ai._ask_openai("sys", one, 700, ["QkFTRTY0"])
    body = http.calls[-1][1]
    content = body["messages"][-1]["content"]
    check("openai: картинка как data-url",
          content[0]["type"] == "image_url"
          and content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
    check("openai: текст следом за картинкой", content[-1]["text"] == "вопрос")
    check("openai: лимит токенов из настройки", body["max_tokens"] == 700)
    await ai._ask_openai("sys", one, 700)
    check("без картинок content остаётся строкой",
          http.calls[-1][1]["messages"][-1]["content"] == "вопрос")
    check("системный промпт идёт отдельной ролью",
          http.calls[-1][1]["messages"][0]["role"] == "system")
    ai._client = None

    # --- диалог ходами вместо простыни текста ---
    rows_d = [Line("@vasya", "почём яблоки"), Line(ai.SELF, "дороже вчерашнего"),
              Line("@petya", "грабёж"), Line("@petya", "но возьму")]
    t = ai.turns(rows_d)
    check("чужие реплики — ход user", t[0] == {"role": "user",
                                               "content": "@vasya: почём яблоки"})
    check("свои — ход assistant без подписи",
          t[1] == {"role": "assistant", "content": "дороже вчерашнего"})
    check("подряд идущие одной роли склеены",
          len(t) == 3 and t[2]["content"] == "@petya: грабёж\n@petya: но возьму")
    check("роли строго чередуются",
          all(a["role"] != b["role"] for a, b in zip(t, t[1:])))
    check("реакции едут вместе с репликой",
          "[реакции: 🔥]" in ai.turns([Line("@v", "шутка", 1, None, None, "🔥")])[0]["content"])

    # --- примеры реплик из карточки ---
    await db.set_setting(CID, "ai_examples",
                         "собеседник: почём яблоки?\nты: дороже, чем вчера.")
    s = await db.get_settings(CID)
    ex = ai.examples(s)
    check("примеры разобрались", len(ex) == 2 and ex[1].who == ai.SELF)
    check("в промпте про них сказано", ai._EXAMPLE_NOTE in ai._prompt(s, "Чат", "@v"))

    sent = {}

    async def spy(system, messages, tokens=0, images=None):
        sent["s"], sent["m"] = system, messages
        return "ответ"

    ai._ask_ollama = spy
    await ai.ask(s, "Чат", CID, "@vasya", "а сегодня?", snapshot=[])
    # Примеры идут без ярлыка «собеседник: ». Роль user уже говорит, кто это,
    # а ярлык модель принимала за формат ответа: писала им сцены за обе
    # стороны и цитировала сами примеры дословно.
    check("примеры идут первыми ходами и без ярлыка",
          sent["m"][0]["content"] == "почём яблоки?")
    check("ответ персонажа в примере — ход assistant",
          sent["m"][1] == {"role": "assistant", "content": "дороже, чем вчера."})
    check("задание идёт последним ходом",
          sent["m"][-1]["role"] == "user"
          and "Отвечай на последнюю реплику" in sent["m"][-1]["content"])
    check("простыни <chat> больше нет", "<chat>" not in ai.flatten(sent["m"]))

    await db.set_setting(CID, "ai_examples", None)
    s = await db.get_settings(CID)
    check("без примеров пояснения о них нет",
          ai._EXAMPLE_NOTE not in ai._prompt(s, "Чат", "@v"))

    # примеры из настоящей карточки chub: раньше это поле выбрасывалось
    from slusha import lore as lorem
    card = lorem.parse_card({"data": {
        "name": "Holo", "description": "Мудрая волчица.",
        "mes_example": "<START>\n{{user}}: почём яблоки?\n"
                       "{{char}}: дороже вчерашнего.\n"
                       "<START>\n{{user}}: ты пьяна?\n{{char}}: вовсе нет!"}})
    check("mes_example разобран", card["examples"].count("\n") == 3)
    check("герой подписан как «ты»", card["examples"].startswith("собеседник: почём"))
    check("плейсхолдеры не просочились", "{{" not in card["examples"])
    done = await lorem.apply_card(CID, card)
    check("импорт кладёт примеры в настройки", "примеры реплик" in done)
    check("и они видны боту",
          len(ai.examples(await db.get_settings(CID))) == 4)
    await db.set_setting(CID, "ai_examples", None)

    # --- фоновый лор выключается ---
    from slusha import lore
    await db.lore_add(CID, "wheat, harvest", "Пшеница родит раз в год. " * 30)
    hit = await lore.block(CID, "расскажи про wheat")
    check("по совпавшему ключу лор просыпается", bool(hit))
    bg = await lore.block(CID, "привет как дела", background=True)
    check("без совпадений фон подмешивается", bool(bg))
    off = await lore.block(CID, "привет как дела", background=False)
    check("и выключается тумблером", off == "")
    always = await lore.block(CID, "привет как дела", background=False)
    check("выключатель не трогает совпавшее по ключу",
          bool(await lore.block(CID, "про wheat речь", background=False)) and always == "")
    await db.lore_clear(CID)

    # --- совпавшее по ключу идёт вперёд фона ---
    # Записи «всегда» раньше сваливались в одну кучу с совпавшими и по prio
    # съедали бюджет целиком: то, что реально относилось к разговору, до
    # модели не доезжало.
    await db.lore_clear(CID)
    for i in range(6):
        await db.lore_add(CID, "", "Постоянная справка о мире номер %d. " % i * 25,
                          always=1, prio=1)
    await db.lore_add(CID, "пиво", "В баре наливают тёмное, по три монеты." * 8,
                      always=0, prio=99)
    block = await lore.block(CID, "кто пойдёт за пивом", background=False)
    check("совпавшее по ключу попало в промпт", "тёмное" in block)
    check("и стоит первым, до фона",
          block.index("тёмное") < block.index("Постоянная"))
    check("фон занимает остаток бюджета", "Постоянная" in block)
    only_bg = await lore.block(CID, "погода дрянь", background=False)
    check("без совпадений остаётся один фон",
          "Постоянная" in only_bg and "тёмное" not in only_bg)
    check("бюджет соблюдён", len(block) <= config.LORE_BUDGET + 200)
    await db.lore_clear(CID)

    # --- рамки промпта стали короче и без запретов ---
    frames = ai._FRAME_STYLE + ai._FRAME_STRICT
    check(f"рамки укоротились ({len(frames)} знаков)", len(frames) < 800)
    check("запретов в стиле не осталось",
          "не повторяй" not in ai._FRAME_STYLE.lower()
          and "без списков" not in ai._FRAME_STYLE.lower())

    # --- сколько своих имён показываем модели ---
    # Ловим сообщения по всему списку, а в промпт кладём немного: длинный
    # перечень модель зачитывает вслух и берёт слова оттуда как обращение —
    # в чат ушло «Ты хоть понимаешь, о чём говоришь, неко?».
    many = ["ТабаКошка", "яни", "кошка", "таба", "табакошка", "неко"]
    shown = ai._prompt(s, "Чат", "@v", many)
    line = next(ln for ln in shown.split("\n") if ln.startswith("Тебя зовут:"))
    listed = [n.strip() for n in line.split(":", 1)[1].split(".")[0].split(",")]
    check("имён в промпте не больше потолка", len(listed) == config.AI_NAMES_SHOWN)
    check("показаны первые по порядку", listed == many[:config.AI_NAMES_SHOWN])
    check("хвост списка в промпт не уехал", "неко" not in listed)
    # Дубли через ё/е занимали два места из трёх: «Декамарт, вахоёб, вахоеб».
    dbl = ai._prompt(s, "Чат", "@v", ["Декамарт", "вахоёб", "вахоеб", "дека"])
    dline = next(ln for ln in dbl.split("\n") if ln.startswith("Тебя зовут:"))
    check("ё и е считаются одним именем",
          "вахоеб" not in dline.split(".")[0] and "дека" in dline)
    # --- подпись говорящего в своём же ответе ---
    # Модель видит чужие реплики как «@ник: текст» и копирует формат на себя.
    # В живом чате приходило «**@thatmossybot (Холо):** дороже, чем вчера».
    names = ["@slusha_bot", "Слюша", "холо"]
    check("markdown, юзернейм и имя в скобках срезаны",
          ai.strip_bot_prefix("**@slusha_bot (Слюша):**  \nдороже вчерашнего", names)
          == "дороже вчерашнего")
    check("простая подпись срезана",
          ai.strip_bot_prefix("@slusha_bot: привет", names) == "привет")
    check("имя персонажа тоже",
          ai.strip_bot_prefix("Слюша: сегодня дождь", names) == "сегодня дождь")
    check("служебное «ты» тоже", ai.strip_bot_prefix("ты: ответ", names) == "ответ")
    check("чужую подпись не трогаем",
          ai.strip_bot_prefix("@vasya: это его слова", names) == "@vasya: это его слова")
    check("обычный текст не портим",
          ai.strip_bot_prefix("просто ответ", names) == "просто ответ")

    # Подпись со звёздочками пришла в чат сотнями реплик: имя ловилось
    # вместе с «**» и не узнавалось как своё, а из чата уезжало в историю.
    check("звёздочки прилипли к имени — всё равно срезаем",
          ai.strip_bot_prefix("Декамарт**: смотря как посмотреть",
                              ["Декамарт"]) == "смотря как посмотреть")
    check("ник с подчёркиванием не ломает разбор",
          ai.strip_bot_prefix("**@slusha_bot**: привет", names) == "привет")

    # Модель разыгрывает сцену за обе стороны: сначала реплика «собеседника»
    # (наш же ярлык из примеров), потом своя. Чужую половину выбрасываем.
    check("выдуманная реплика собеседника выброшена",
          ai.strip_bot_prefix("собеседник**: ты кто?\n**Слюша**: архимагос.",
                              names) == "архимагос.")
    # А свои продолжения — бормотание вперёд: в чате их читать нечем.
    check("дописанные за себя ходы отброшены",
          ai.strip_bot_prefix("Слюша: раз\nСлюша: два\nСлюша: три",
                              names) == "раз")
    check("многострочный ответ без подписей цел",
          ai.strip_bot_prefix("первая строка\nвторая строка", names)
          == "первая строка\nвторая строка")

    # --- указание из промпта, выданное за реплику ---
    # В чат ушло сообщение «(одной-двумя короткими фразами)» — дословный
    # текст правила об объёме ответа.
    rule = config.AI_LEN_RULES[0][0]
    check("правило объёма не уходит в чат",
          ai.strip_orders(f"({rule})") == "")
    check("правило отдельной строкой вырезано",
          ai.strip_orders(f"Гав?\n({rule})") == "Гав?")
    check("обычный текст в скобках не трогаем",
          ai.strip_orders("(шутка)") == "(шутка)")

    # --- ответ целиком в кавычках ---
    check("кавычки вокруг всего ответа сняты",
          ai.strip_quotes("\"Айфон — это же зарядка\"")
          == "Айфон — это же зарядка")
    check("ёлочки тоже", ai.strip_quotes("«ответ»") == "ответ")
    check("цитата внутри ответа цела",
          ai.strip_quotes("\"да\" — сказал он, \"нет\"")
          == "\"да\" — сказал он, \"нет\"")

    # --- вцепившаяся присказка ---
    # Три ответа подряд с «археотехнологиями»: тексты разные, repeats()
    # молчит, а в чате одно и то же.
    mine = ["я откалибровал параметр и добавил в него археотехнологии",
            "я проверил твой код и добавил в него археотехнологии"]
    theirs = ["гав?", "о декамарт родненький", "че он такой разговорчивый"]
    check("присказка поймана",
          ai.hooked("я добавлю в него ещё больше археотехнологий", mine, theirs)
          .startswith("археотехнолог"))
    # Тему разговора трогать нельзя: её задают собеседники.
    topic_mine = ["айфон это же ещё и зарядка", "ты ещё и зарядкой займёшь?"]
    topic_theirs = ["что думаешь об айфонах?", "зарядку к ним докупать надо"]
    check("тему разговора за присказку не считаем",
          ai.hooked("зарядка у айфона вечно дохнет", topic_mine, topic_theirs) == "")
    check("свежий ответ не трогаем",
          ai.hooked("да мне похуй на телефоны", topic_mine, topic_theirs) == "")
    # --- повтор своих же слов ---
    check("дословный повтор пойман",
          ai.repeats("спасибо брат что не забыл", ["спасибо, брат, что не забыл."]))
    check("почти дословный тоже",
          ai.repeats("дороже чем вчера и дешевле чем завтра",
                     ["дороже, чем вчера, и дешевле, чем завтра. Бери сейчас"]))
    check("другая мысль повтором не считается",
          not ai.repeats("а пойдём лучше за пивом", ["спасибо, брат, что не забыл."]))
    check("пустой ответ повтором не считается", not ai.repeats("", ["что угодно"]))

    said = ai._said_before([Line("@vasya", "чужое"), Line(ai.SELF, "моё старое")],
                           [Line(ai.SELF, "строка примера"),
                            Line("собеседник", "вопрос примера")])
    check("для проверки берём свои реплики и примеры",
          said == ["моё старое", "строка примера"])

    # бот повторился — переспрашиваем один раз
    answers = iter(["одно и то же", "а вот это уже другое"])
    calls = []

    async def parrot(system, messages, tokens=0, images=None):
        calls.append(messages)
        return next(answers)

    ai._ask_ollama = parrot
    await db.set_setting(CID, "ai_examples", None)
    s = await db.get_settings(CID)
    hist = [Line("@vasya", "спасибо"), Line(ai.SELF, "одно и то же")]
    parts = await ai.ask(s, "Чат", CID, "@vasya", "спасибо ещё раз", snapshot=hist)
    check("после повтора бот переспросил", len(calls) == 2)
    check("и отдал в чат уже другое", parts == ["а вот это уже другое"])
    check("в переспросе модели показали её же ответ",
          calls[1][-2]["content"] == "одно и то же"
          and "уже говорил" in calls[1][-1]["content"])

    calls.clear()
    answers = iter(["одно и то же", "одно и то же"])
    parts = await ai.ask(s, "Чат", CID, "@vasya", "спасибо ещё раз", snapshot=hist)
    check("если и со второй попытки повтор — молчим", parts == [])

    # --- эхо вопроса в начале ответа ---
    # Со скриншота из живого чата: «коул что думаешь? я думаю так: сосиски —
    # основа…». Целевая реплика лежит в промпте дважды — строкой переписки и
    # дословно в задании, — и модель порой читает это как «продолжи строку».
    check("эхо срезано",
          ai.strip_echo("коул что думаешь? я думаю так: сосиски — основа.",
                        "коул что думаешь?") == "я думаю так: сосиски — основа.")
    check("другая пунктуация и регистр тоже",
          ai.strip_echo("Коул, что думаешь?! дальше ответ", "коул что думаешь?")
          == "дальше ответ")
    check("ответ без эха не трогаем",
          ai.strip_echo("я думаю так: сосиски основа", "коул что думаешь?")
          == "я думаю так: сосиски основа")
    check("похожее начало, но не тот вопрос",
          ai.strip_echo("что думаешь о погоде? хороший вопрос", "коул что думаешь?")
          == "что думаешь о погоде? хороший вопрос")

    async def echoer(system, messages, tokens=0, images=None):
        return "коул что думаешь? отвечаю по существу"

    ai._ask_ollama = echoer
    parts = await ai.ask(s, "Чат", CID, "@misha", "коул что думаешь?",
                         snapshot=[Line("@misha", "коул что думаешь?")])
    check("эхо не доезжает до чата", parts == ["отвечаю по существу"])

    # --- подпись собеседника в своём же ответе ---
    # Со скриншота MlinBot: «@Just_Kaiser767: Кого его? Я же говорила…», где
    # @Just_Kaiser767 — тот, кто спросил. Раньше резались только свои имена,
    # и чужой ник проходил, а следом за ним не срезалось и эхо вопроса.
    async def copycat(system, messages, tokens=0, images=None):
        return "@kaiser: кого его? я же говорила, что мы спасём этот день"

    ai._ask_ollama = copycat
    parts = await ai.ask(s, "зашкафье", CID, "@kaiser", "кого его",
                         ["@slusha_bot"],
                         snapshot=[Line("@kaiser", "кого его", 1)])
    check("ник собеседника срезан вместе с эхом",
          parts == ["я же говорила, что мы спасём этот день"])

    check("имя участника разговора режется",
          ai.strip_bot_prefix("@kaiser: привет", ["@slusha_bot", "@kaiser"])
          == "привет")
    check("имя постороннего не режется",
          ai.strip_bot_prefix("Гагарин: поехали", ["@slusha_bot", "@kaiser"])
          == "Гагарин: поехали")

    # --- название чата из системного промпта ---
    # Со скриншота: «овощехранилище 🛩 — и ты, и я, мы оба в одном месте…».
    # В системном промпте есть строка «Чат: «овощехранилище»», и модель берёт
    # её началом ответа — так же, как брала подпись и вопрос.
    check("название со смайликом и тире срезано",
          ai.strip_echo("овощехранилище 🛩 — и ты, и я", "овощехранилище 🛩",
                        need_sep=True) == "и ты, и я")
    check("и через двоеточие тоже",
          ai.strip_echo("овощехранилище: тут всё просто", "овощехранилище",
                        need_sep=True) == "тут всё просто")
    check("но осмысленную фразу с тем же словом не портим",
          ai.strip_echo("овощехранилище хранит овощи", "овощехранилище",
                        need_sep=True) == "овощехранилище хранит овощи")

    async def titler(system, messages, tokens=0, images=None):
        return "овощехранилище 🛩 — и ты, и я, мы оба в одном месте"

    ai._ask_ollama = titler
    parts = await ai.ask(s, "овощехранилище 🛩", CID, "@misha", "коул, рецепт пирога?",
                         snapshot=[Line("@misha", "коул, рецепт пирога?")])
    check("название чата не доезжает до чата",
          parts == ["и ты, и я, мы оба в одном месте"])

    # --- свои повторы не показываем модели ---
    stuck = [Line("@v", "раз"), Line(ai.SELF, "залипшая фраза"),
             Line("@v", "два"), Line(ai.SELF, "залипшая фраза"),
             Line("@v", "три"), Line(ai.SELF, "залипшая фраза")]
    t = ai.turns(stuck)
    check("залипшая реплика показана один раз",
          sum(1 for m in t if m["role"] == "assistant") == 1)
    check("чужие реплики при этом целы",
          all(w in ai.flatten(t) for w in ("раз", "два", "три")))
    check("роли всё ещё чередуются",
          all(a["role"] != b["role"] for a, b in zip(t, t[1:])))

    # --- из немоты есть выход ---
    pair = iter(["залипшая фраза", "залипшая фраза, но чуть иначе сказанная тут"])

    async def stubborn(system, messages, tokens=0, images=None):
        return next(pair)

    ai._ask_ollama = stubborn
    parts = await ai.ask(s, "Чат", CID, "@v", "ну что",
                         snapshot=[Line(ai.SELF, "залипшая фраза"), Line("@v", "ну что")])
    check("похожий, но не дословный ответ всё же уходит в чат", parts != [])

    pair = iter(["залипшая фраза", "залипшая фраза"])
    ai._ask_ollama = stubborn
    parts = await ai.ask(s, "Чат", CID, "@v", "ну что",
                         snapshot=[Line(ai.SELF, "залипшая фраза"), Line("@v", "ну что")])
    check("а дословный повтор по-прежнему проглатывается", parts == [])

    # --- задание не отдельным ходом ---
    calls.clear()
    answers = iter(["нормальный ответ"])
    ai._ask_ollama = parrot          # выше заглушку меняли на другую
    await ai.ask(s, "Чат", CID, "@vasya", "как дела", snapshot=[Line("@vasya", "как дела")])
    msgs = calls[0]
    check("двух user подряд нет",
          all(a["role"] != b["role"] for a, b in zip(msgs, msgs[1:])))
    check("задание приклеено к последней реплике",
          "Отвечай на последнюю реплику" in msgs[-1]["content"]
          and "@vasya: как дела" in msgs[-1]["content"])
    check("вопрос в промпте ровно один раз",
          ai.flatten(msgs).count("как дела") == 1)

    # --- запас токенов думающей модели даётся всегда ---
    was_model, was_hush = config.AI_MODEL, config.AI_NO_THINK
    config.AI_MODEL, config.AI_NO_THINK = "qwen3.5:4b", True
    try:
        check("думающей модели запас нужен даже с выключателем",
              ai.max_tokens(s) > config.AI_LEN_TOKENS[s.ai_len])
    finally:
        config.AI_MODEL, config.AI_NO_THINK = was_model, was_hush

    ai._ask_ollama = real_ollama

    # --- /no_think в тексте больше не шлём ---
    http = FakeHttp()
    ai._client = http
    config.AI_MODEL = "qwen3.5:4b"
    try:
        await ai._ask_ollama("характер", [{"role": "user", "content": "вопрос"}], 700)
        body = http.calls[-1][1]
        check("поле think выключает размышления", body.get("think") is False)
        check("текстовой пометки в промпте нет",
              "/no_think" not in ai.flatten(body["messages"]))
    finally:
        config.AI_MODEL = was_model
        ai._client = None

    # --- как читается контекст ---
    # Со скриншота: «брух» и «Он меня любит» отвечали боту, а модель видела их
    # подряд и считала, что второй отвечает первому. Связи реплаев в промпт
    # не попадали вовсе, хотя в базе лежали.
    ctx = [Line("@misha", "сосите бибу", 1),
           Line(ai.SELF, "мой ответ", 2, 1),
           Line("@misha", "брух", 3, 2),
           Line("@sina", "он меня любит", 4, 2)]
    flat = ai.flatten(ai.turns(ctx))
    # «брух» отвечает боту и идёт сразу за его ходом — подпись не нужна,
    # порядок ходов и так это говорит. Пересказ съеденного хода только путал
    # модель: её собственные слова оказывались внутри чужой реплики.
    check("ответ на предыдущий ход не пересказывается",
          "@misha: брух" in flat)
    # А вот «он меня любит» идёт уже после чужой реплики — без подписи модель
    # решит, что отвечают ей, а не боту. Это и был баг со скриншота.
    check("ответ вглубь подписан «в ответ тебе»",
          "(в ответ тебе: «мой ответ»)" in flat)
    check("подпись ровно одна — только там, где она нужна",
          flat.count("(в ответ тебе") == 1)
    check("ответ человеку через голову соседа подписан его ником",
          "(в ответ @misha: «сосите бибу»)"
          in ai.flatten(ai.turns([Line("@misha", "сосите бибу", 1),
                                  Line("@sina", "не тебе", 2),
                                  Line("@kolya", "ага", 3, 1)])))
    check("реплика без реплая подписана просто ником",
          ai.turns([Line("@misha", "просто так", 1)])[0]["content"]
          == "@misha: просто так")

    # серия одинаковых вложений — одной строкой
    photos = [Line("@m", "[фото]", 10 + i) for i in range(5)]
    photos.append(Line("@s", "какие котики", 20, 12))     # ответ на среднюю
    flat = ai.flatten(ai.turns(photos))
    check("пять фото схлопнуты в одну строку", "[фото] ×5" in flat)
    check("и остались одной строкой, а не парами", "×2" not in flat)
    check("связь реплая на схлопнутое сообщение цела",
          "(в ответ @m: «[фото]»)" in flat)

    # метка долгой паузы
    now = 1_700_000_000
    gap = [Line("@m", "вчерашнее", 1, None, None, "", now - 4 * 3600),
           Line("@m", "сегодняшнее", 2, None, None, "", now)]
    check("долгая пауза помечена", "[прошло 4 ч.]" in ai.flatten(ai.turns(gap)))
    close = [Line("@m", "раз", 1, None, None, "", now),
             Line("@m", "два", 2, None, None, "", now + 60)]
    check("короткая пауза не помечается", "прошло" not in ai.flatten(ai.turns(close)))

    # однословное эхо
    check("однословное эхо с тире срезано",
          ai.strip_echo("брух — но я не забыл", "брух") == "но я не забыл")
    check("однословное эхо без разделителя не трогаем",
          ai.strip_echo("почему бы и нет", "почему") == "почему бы и нет")

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
