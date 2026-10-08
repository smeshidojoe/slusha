"""Бот ставит реакции под сообщениями — там, где словами не ответил.

«Спасибо» в ответ боту, шутка в общем разговоре — живой участник на такое
чаще ставит смайлик, чем пишет. Ответ словами по-прежнему решает ai.py; сюда
приходят только сообщения, на которые бот промолчал.

Как выбираем реакцию. Модель спрашиваем не «какую реакцию ты поставишь», а
«что это за сообщение» — шутка, благодарность, новость, — и эмодзи берём по
таблице. На живых сообщениях трёх чатов gemma3:4b с характером и списком
эмодзи ставила почти на всё одно и то же: Коул и Яни — 🤡, Холо — 😏 (которого
нет среди разрешённых ботам реакций). Без характера, с вопросом про вид
сообщения — «Спасибо» ❤, «капец ты токсичный» 🤡, «ложусь на операцию» 😱, а
вопросы и обрывки фраз честно уходят в «обычную реплику» — без реакции.

Права. Ставить реакции админка не нужна. Но чат может разрешить только
часть реакций или выключить их вовсе, а сообщение — пропасть; любая ошибка
Telegram здесь — повод промолчать, а не ругаться в лог. Чат, где реакция не
прошла, на время оставляем в покое.
"""
import asyncio
import logging
import random
import time

from . import config, utils

logger = logging.getLogger("slusha.emote")

# что это за сообщение — и какой реакцией на такое отвечают; «-» — никакой.
# Обычная реплика и вопрос первыми: на них модель и должна молчать чаще всего.
_TO_BOT = [("обычная реплика", "-"), ("вопрос", "-"), ("шутка", "😁"),
           ("согласие", "👍"), ("благодарность", "❤"), ("комплимент тебе", "❤"),
           ("оскорбление тебя", "🤡"), ("грусть или неудача", "😢")]
_IN_CHAT = [("обычная реплика", "-"), ("вопрос", "-"), ("шутка", "😁"),
            ("радость или успех", "🔥"), ("грусть или неудача", "😢"),
            ("новость", "👀"), ("жуть или шок", "😱"), ("праздник", "🎉")]

_SYSTEM = "Ты участник группового чата."
_TASK = ("{ctx}Сообщение от {who}: «{text}»\n\n"
         "Что это за сообщение? Варианты: {kinds}.\n"
         "Ответь только вариантом из списка.")

# чат, где реакция не прошла (выключены, запрещены), — не трогаем столько секунд
_REST = 6 * 3600
# сколько держим список разрешённых в чате реакций
_ALLOWED_TTL = 6 * 3600

_last: dict[int, float] = {}                     # последняя реакция в чате
_count: dict[int, tuple[int, int]] = {}          # чат -> (день, реакций за день)
_rest: dict[int, float] = {}                     # чат -> до какого времени молчим
_allowed: dict[int, tuple[float, set | None]] = {}
_busy: set[int] = set()


def chance(s, to_bot: bool) -> int:
    """Шанс поставить реакцию, в процентах."""
    level = getattr(s, "ai_react", 1)
    to, other = config.AI_REACT_CHANCE.get(level, (0, 0))
    return to if to_bot else other


def kind(out: str, table) -> str:
    """Ответ модели — в эмодзи. Пусто — реакции не будет."""
    low = (out or "").lower()
    for name, emoji in table:
        if name in low:
            return "" if emoji == "-" else emoji
    return ""


def _spent(chat_id: int) -> int:
    day, n = _count.get(chat_id, (0, 0))
    return n if day == utils.day_num() else 0


def _may(chat_id: int, to_bot: bool, now: float) -> bool:
    if _rest.get(chat_id, 0) > now or chat_id in _busy:
        return False
    if _spent(chat_id) >= config.AI_REACT_DAILY:
        return False
    # в общем разговоре — не чаще раза в AI_REACT_GAP; ответ боту реже минуты
    # тоже не нужен: иначе обмен стикерами-смайликами превращается в ленту
    gap = 60 if to_bot else config.AI_REACT_GAP
    return now - _last.get(chat_id, 0) >= gap


async def _chat_allows(bot, chat_id: int) -> set | None:
    """Какие реакции разрешены в чате. None — все; пустое множество — никакие."""
    now = time.time()
    hit = _allowed.get(chat_id)
    if hit and now - hit[0] < _ALLOWED_TTL:
        return hit[1]
    allowed = None
    try:
        chat = await bot.get_chat(chat_id)
        got = getattr(chat, "available_reactions", None)
        if got is not None:
            allowed = {getattr(r, "emoji", "") for r in got if getattr(r, "type", "") == "emoji"}
    except Exception:
        logger.debug("не узнать реакции чата %s", chat_id, exc_info=True)
    _allowed[chat_id] = (now, allowed)
    return allowed


async def pick(s, who: str, text: str, theirs: str | None) -> str:
    """Какую реакцию поставить. Пусто — никакую."""
    from . import ai
    table = _TO_BOT if theirs is not None else _IN_CHAT
    ctx = f"Ты писал в чат: «{theirs[:200]}»\nВ ответ тебе:\n" if theirs else ""
    task = _TASK.format(ctx=ctx, who=who, text=text[:300],
                        kinds="; ".join(name for name, _ in table))
    tokens = 16 + (config.AI_THINKING_RESERVE if ai._reserve_needed() else 0)
    out = await ai.raw(_SYSTEM, task, tokens)
    return kind(ai.strip_thoughts(out or ""), table)


async def _put(bot, chat_id: int, msg_id: int, emoji: str) -> bool:
    from aiogram.types import ReactionTypeEmoji
    allowed = await _chat_allows(bot, chat_id)
    if allowed is not None and emoji not in allowed:
        return False
    try:
        await bot.set_message_reaction(chat_id, msg_id, [ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:
        # реакции выключены, запрещены, сообщение пропало, нет прав — всё
        # это не поломка, а «здесь нельзя»; чат пока оставляем в покое
        logger.info("реакция %s в чате %s не прошла: %s", emoji, chat_id, e)
        if not utils.msg_gone(e) and "not found" not in str(e).lower():
            _rest[chat_id] = time.time() + _REST
        return False
    return True


async def _run(bot, s, chat_id: int, msg_id: int, who: str, text: str,
               theirs: str | None) -> None:
    try:
        emoji = await pick(s, who, text, theirs)
        if not emoji:
            return
        if await _put(bot, chat_id, msg_id, emoji):
            day = utils.day_num()
            _count[chat_id] = (day, _spent(chat_id) + 1)
            _last[chat_id] = time.time()
            logger.info("реакция %s в чате %s на «%s»", emoji, chat_id,
                        text[:60].replace("\n", " "))
    except Exception:
        logger.warning("реакция в чате %s сорвалась", chat_id, exc_info=True)
    finally:
        _busy.discard(chat_id)


async def maybe(bot, message, s) -> bool:
    """Бот промолчал на сообщение — может, поставить реакцию. В фоне.

    Вернуть True, если задача поставлена (сама реакция может и не случиться).
    """
    from . import ai
    text = (message.text or message.caption or "").strip()
    user = message.from_user
    if not text or text.startswith(("/", "!")) or user is None or user.is_bot:
        return False
    if not s.ai_on or not ai.available():
        return False
    to_bot = await ai.replied_to(bot, message)
    if not to_bot and ai.noisy(text):
        return False                     # «ага» в чужом разговоре — мимо
    if random.random() * 100 >= chance(s, to_bot):
        return False
    chat_id = message.chat.id
    now = time.time()
    if not _may(chat_id, to_bot, now):
        return False
    if ai.capped() and await ai.spent_today(chat_id) >= s.ai_daily:
        return False                     # платная модель: лимит дня исчерпан
    theirs = None
    if to_bot:
        reply = message.reply_to_message
        theirs = (reply.text or reply.caption or "").strip()
    who = (user.username and f"@{user.username}") or user.full_name
    _busy.add(chat_id)
    _last[chat_id] = now                 # пока модель думает, второй раз не берёмся
    asyncio.create_task(_run(bot, s, chat_id, message.message_id, who, text, theirs))
    return True
