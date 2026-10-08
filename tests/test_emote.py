"""Бот ставит реакции там, где не ответил словами (emote.py).

Модель подменена: проверяем, когда бот ставит реакцию, какую, и что ошибки
Telegram (реакции выключены, нет прав, сообщение пропало) не ломают ничего.
"""
import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-emote-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_HISTORY_DB=os.path.join(TMP, "h.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b", AI_COLLECT_MODEL="", MEM_NOTES="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db, emote                      # noqa: E402
from slusha import history as store                           # noqa: E402

CID = -100778
BOT_ID = 1000
FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    def __init__(self, error=None, allowed=None):
        self.reacted = []
        self.sent = []
        self.error = error
        self.allowed = allowed

    async def me(self):
        return SimpleNamespace(id=BOT_ID, username="slusha_bot", full_name="Слюша")

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, available_reactions=self.allowed)

    async def set_message_reaction(self, chat_id, message_id, reaction):
        if self.error:
            raise self.error
        self.reacted.append((chat_id, message_id, [r.emoji for r in reaction]))
        return True

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=900 + len(self.sent))


def msg(text, who="vasya", uid=7, mid=1, to_bot=None):
    reply = None
    if to_bot is not None:
        reply = SimpleNamespace(message_id=mid - 1, text=to_bot, caption=None,
                                from_user=SimpleNamespace(id=BOT_ID, is_bot=True))
    return SimpleNamespace(
        chat=SimpleNamespace(id=CID, title="Чат", type="supergroup"),
        from_user=SimpleNamespace(id=uid, username=who, full_name=who, is_bot=False),
        text=text, caption=None, message_id=mid, reply_to_message=reply,
        is_topic_message=False, message_thread_id=None)


def reset():
    emote._last.clear()
    emote._count.clear()
    emote._rest.clear()
    emote._allowed.clear()
    emote._busy.clear()


async def settle():
    """Дождаться фоновой задачи с реакцией."""
    for _ in range(50):
        if not emote._busy:
            return
        await asyncio.sleep(0.01)


async def main():
    await db.init()
    await db.upsert_chat(CID, "Чат", None, 424211817)
    await db.set_setting(CID, "ai_on", 1)
    await db.set_setting(CID, "ai_random", 0)        # словами в разговор не лезет
    await db.set_setting(CID, "ai_reply", 0)         # и на реплаи не отвечает
    s = await db.get_settings(CID)
    check("по умолчанию — изредка", s.ai_react == 1)

    answer = {"text": "Благодарность"}
    asked = []

    async def fake(system, messages, tokens, images):
        asked.append(messages[-1]["content"])
        return answer["text"]
    ai._ask_ollama = fake
    emote.random = SimpleNamespace(random=lambda: 0.0)                 # шанс всегда срабатывает

    # --- разбор ответа модели ---
    check("«Благодарность» — ❤", emote.kind("Благодарность.", emote._TO_BOT) == "❤")
    check("«обычная реплика» — без реакции", emote.kind("Обычная реплика", emote._TO_BOT) == "")
    check("болтовня — без реакции", emote.kind("Ну, я думаю...", emote._IN_CHAT) == "")
    check("«оскорбление тебя» только в ответ боту",
          emote.kind("оскорбление тебя", emote._IN_CHAT) == "")

    # --- «спасибо» в ответ боту, словами бот не ответил ---
    bot = FakeBot()
    reset()
    await ai.maybe_reply(bot, msg("спасибо", mid=11, to_bot="Держи рецепт пирога."), s)
    await settle()
    check("на «спасибо» боту — ❤ на то самое сообщение", bot.reacted == [(CID, 11, ["❤"])])
    check("словами не ответил", not bot.sent)
    check("модель видела реплику бота", asked and "Держи рецепт пирога" in asked[-1])
    check("и вариант «благодарность»", asked and "благодарность" in asked[-1])

    # --- обычная реплика — без реакции ---
    bot = FakeBot()
    reset()
    answer["text"] = "Обычная реплика"
    await ai.maybe_reply(bot, msg("ну я до сих пор с парной авой", mid=12), s)
    await settle()
    check("обычная реплика — без реакции", not bot.reacted)

    # --- в общем разговоре: шутка, потом пауза ---
    bot = FakeBot()
    reset()
    answer["text"] = "Шутка"
    await ai.maybe_reply(bot, msg("запить его ягерьместером", mid=13), s)
    await settle()
    await ai.maybe_reply(bot, msg("а потом ещё и текилой сверху", mid=14), s)
    await settle()
    check("шутка в чате — 😁", bot.reacted == [(CID, 13, ["😁"])])
    check("вторая подряд — нет: пауза AI_REACT_GAP", len(bot.reacted) == 1)

    # --- шум и команды ---
    bot = FakeBot()
    reset()
    await ai.maybe_reply(bot, msg("ага", mid=15), s)
    await ai.maybe_reply(bot, msg("/start", mid=16), s)
    await settle()
    check("«ага» и команды в чате — мимо", not bot.reacted)

    # --- шанс не выпал ---
    bot = FakeBot()
    reset()
    emote.random = SimpleNamespace(random=lambda: 0.99)
    await ai.maybe_reply(bot, msg("запить его ягерьместером", mid=17), s)
    await settle()
    check("шанс не выпал — без реакции", not bot.reacted)
    emote.random = SimpleNamespace(random=lambda: 0.0)

    # --- в чате разрешены не все реакции ---
    bot = FakeBot(allowed=[SimpleNamespace(type="emoji", emoji="👍")])
    reset()
    await ai.maybe_reply(bot, msg("запить его ягерьместером", mid=18), s)
    await settle()
    check("😁 в чате запрещён — не ставим", not bot.reacted)
    bot = FakeBot(allowed=[])
    reset()
    await ai.maybe_reply(bot, msg("запить его ягерьместером", mid=19), s)
    await settle()
    check("реакции в чате выключены — не ставим", not bot.reacted)

    # --- ошибки Telegram: молчим, чат оставляем в покое ---
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    from aiogram.methods import SetMessageReaction
    method = SetMessageReaction(chat_id=CID, message_id=1)
    for name, err in [("REACTION_INVALID", TelegramBadRequest(method, "Bad Request: REACTION_INVALID")),
                      ("нет прав", TelegramForbiddenError(method, "Forbidden: not enough rights"))]:
        bot = FakeBot(error=err)
        reset()
        await ai.maybe_reply(bot, msg("запить его ягерьместером", mid=20), s)
        await settle()
        calls = len(asked)
        await ai.maybe_reply(bot, msg("спасибо", mid=22, to_bot="Держи."), s)
        await settle()
        check(f"{name}: без исключения, и чат на время в покое",
              CID in emote._rest and len(asked) == calls)
    bot = FakeBot(error=TelegramBadRequest(method, "Bad Request: message to react not found"))
    reset()
    await ai.maybe_reply(bot, msg("запить его ягерьместером", mid=23), s)
    await settle()
    check("сообщение пропало — чат не наказываем", CID not in emote._rest)

    # --- лимит в сутки ---
    bot = FakeBot()
    reset()
    answer["text"] = "Благодарность"
    emote._count[CID] = (emote.utils.day_num(), config.AI_REACT_DAILY)
    await ai.maybe_reply(bot, msg("спасибо", mid=31, to_bot="Держи."), s)
    await settle()
    check("лимит в сутки — без реакции", not bot.reacted)

    # --- выключено ---
    await db.set_setting(CID, "ai_react", 0)
    s0 = await db.get_settings(CID)
    bot = FakeBot()
    reset()
    await ai.maybe_reply(bot, msg("спасибо", mid=32, to_bot="Держи."), s0)
    await settle()
    check("выключено — без реакции", not bot.reacted)

    # --- ответил словами — реакцию не ставит ---
    await db.set_setting(CID, "ai_react", 2)
    await db.set_setting(CID, "ai_reply", 100)
    s2 = await db.get_settings(CID)
    bot = FakeBot()
    reset()
    calls = len(asked)
    taken = []
    orig = emote.maybe

    async def spy(*a, **kw):
        taken.append(1)
        return await orig(*a, **kw)
    emote.maybe = spy
    answer["text"] = "Пожалуйста."
    await ai.maybe_reply(bot, msg("спасибо", mid=33, to_bot="Держи."), s2)
    emote.maybe = orig
    check("отвечает словами — реакцию не трогаем", not taken)
    await asyncio.sleep(0.5)                          # пусть ответ словами допишется

    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


code = asyncio.run(main())
sys.stdout.flush()
os._exit(code)
