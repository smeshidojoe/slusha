"""Бот заговаривает первым: спрашивает, как прошло то, что человек собирался.

Планы со сроком ловит plans.py, а спросить про них бот мог только когда
человек сам ему напишет. Пишут боту редко: на корпусах трёх чатов ни один из
трёх пойманных планов до вопроса не дожил — хозяин плана в окне вопроса к
боту так и не обратился. Живой собеседник спросил бы сам.

Как устроено:
- Реплику-вопрос пишем заранее, в фоне, чуть до того, как план созреет, —
  моделью сборщика (AI_COLLECT_MODEL, 12b на процессоре): ей отвечать некогда
  не надо, а вопрос «как прошло» про чужое дело маленькая модель с
  характером пишет плохо. На десяти планах в характере Коула: 4b с
  перепиской спрашивала по делу в 11–15 случаях из 21 — давала совет
  («заверни в поликарбонат»), будто план свежий, или продолжала чужой
  разговор; 12b одним запросом без переписки — 19–20 из 20: «@vasya, так и
  что, Яндекс принял тебя в свои ряды?». Минута процессора на план, а
  планов — несколько в неделю.
- Ступень 1: человек с созревшим планом написал в чат что угодно — бот
  отвечает реплаем на сам план. Как знакомый, увидевший тебя онлайн.
- Ступень 2: человек за AI_INITIATIVE_WAIT так и не появился — бот пишет сам,
  но только днём (AI_INITIATIVE_HOURS), в живой чат (люди писали за последние
  ACTIVE секунд) и не посреди чужой переписки (с последней реплики прошло
  LULL секунд).
- Не больше AI_INITIATIVE_DAILY таких реплик в сутки на чат, и каждая идёт в
  счёт дневного лимита ответов. Про каждый план — один раз.
"""
import asyncio
import logging
import re
import time

from . import config, db, utils

logger = logging.getLogger("slusha.initiative")

# как часто проверять чаты
EVERY = 300
# за сколько до созревания плана готовить вопрос
PREPARE = 2 * 3600
# чат живой: люди писали не раньше, чем столько секунд назад
ACTIVE = 3 * 3600
# и не прямо сейчас: последняя реплика — не позже, чем столько секунд назад
LULL = 10 * 60
# реплика длиннее — это модель рассуждает, а не спрашивает
MAX_LEN = 400

# Без переписки и с одним характером в системе: задание короткое, чтобы
# модель смотрела на план, а не на чат.
# Без «писал(а)»: скобку модель копировала в ответ — «забрал(а)».
_TASK = ("Сообщение {who} в чате: «{text}».\n"
         "Тот день уже прошёл. Ты сам вспомнил об этом и пишешь {who} первым: короткий "
         "вопрос, как всё прошло, — в своём характере, одной-двумя фразами. Назови само "
         "дело. Ответь только своей репликой.")


def gist(text: str) -> str:
    """Предложение плана со сроком.

    «завтра забрать её домой.. но как её тащить в такую холодину, есть идеи?» —
    с вопросом в той же реплике модель бросалась на него отвечать.
    """
    from . import plans
    for part in re.split(r"(?<=[.!?…])\s+|\n+", text):
        if plans._WHEN.search(part):
            return part.strip()
    return text.strip()


def _day_key(chat_id: int) -> str:
    return f"ai_init:{chat_id}:{utils.day_num()}"


async def _spent(chat_id: int) -> int:
    raw = await db.kv_get(_day_key(chat_id))
    return int(raw) if raw and raw.isdigit() else 0


def _on(s, level: int) -> bool:
    return bool(s.ai_on and getattr(s, "ai_plans", 1)
                and getattr(s, "ai_initiative", 2) >= level)


def _daytime(now: int) -> bool:
    from . import plans
    hour = (now + plans.TZ) // 3600 % 24
    lo, hi = config.AI_INITIATIVE_HOURS
    return lo <= hour < hi


async def write(s, chat_id: int, who: str, text: str) -> str:
    """Реплика-вопрос про план. Пусто — не вышло."""
    from . import ai
    persona = (s.ai_persona or config.AI_PERSONA_DEFAULT).strip()
    task = _TASK.format(who=who, text=gist(text)[:300])
    try:
        with ai.patient(config.AI_SUMMARY_TIMEOUT), ai.collecting():
            out = await ai.raw(persona, task, 160)
    except Exception:
        logger.warning("не написать вопрос про план %s в чате %s", who, chat_id, exc_info=True)
        return ""
    out = " ".join(ai.strip_thoughts(out or "").split())
    out = ai.strip_bot_prefix(out, ai.plain_names(s)).strip()
    if len(out) > 2 and out[0] in "«\"" and out[-1] in "»\"":
        out = out[1:-1].strip()
    out = re.sub(r"(?<=\w)\((а|ла|ась)\)", "", out)       # «забрал(а)» — род не угадать
    if not out or len(out) > MAX_LEN or ai.taboo(out):
        logger.info("вопрос про план %s в чате %s не годится: %r", who, chat_id, out[:200])
        return ""
    return out


async def prepare(chat_id: int, s, now: int) -> int:
    """Написать вопросы к планам, что скоро созреют. Вернуть, сколько написали."""
    from . import history as store, plans
    done = 0
    for p in await store.plans_unwritten(chat_id, now + PREPARE, now):
        if not plans.detect(p["text"], p["ts"]):
            # пойман старым словарём: «скидываемся на еду» было «поеду»
            await store.plan_done(p["id"])
            continue
        out = await write(s, chat_id, p["who"], p["text"])
        # '' — не вышло: второй раз не пробуем, бот спросит по-старому,
        # когда человек к нему обратится
        await store.plan_opener(p["id"], out)
        if out:
            done += 1
            logger.info("вопрос про план %s в чате %s готов: %r", p["who"], chat_id, out[:120])
    return done


async def _send(bot, s, chat_id: int, plan: dict, rows) -> bool:
    """Отправить вопрос реплаем на сам план. Сам план после этого закрыт."""
    from aiogram.types import ReplyParameters
    from . import ai, history as store, plans
    await store.plan_done(plan["id"])
    if plans.told(plan["who"], plan["text"], plan["ts"], rows):
        logger.info("план %s в чате %s: уже рассказал сам", plan["who"], chat_id)
        return False
    text = plan["opener"]
    who = plan["who"]
    # реплай на исходное сообщение и так позовёт человека; если оно пропало —
    # зовём по нику
    if not plan["msg_id"] and who.startswith("@") and who.lower() not in text.lower():
        text = f"{who}, {text}"
    extra = {"message_thread_id": plan["thread_id"]} if plan["thread_id"] else {}
    reply = (ReplyParameters(message_id=plan["msg_id"], allow_sending_without_reply=True)
             if plan["msg_id"] else None)
    try:
        sent = await bot.send_message(chat_id, utils.esc(text), reply_parameters=reply, **extra)
    except Exception as e:
        if not utils.msg_gone(e):
            logger.warning("не отправить вопрос про план в %s", chat_id, exc_info=True)
        return False
    await db.kv_set(_day_key(chat_id), str(await _spent(chat_id) + 1))
    await ai._count(chat_id)
    ai._last_reply[chat_id] = time.time()
    await ai.remember(chat_id, ai.SELF, text, getattr(sent, "message_id", None),
                      plan["msg_id"], plan["thread_id"])
    logger.info("сам спросил %s в чате %s про план: %r", who, chat_id, text[:120])
    return True


async def _allowed(s, chat_id: int) -> bool:
    from . import ai
    if await _spent(chat_id) >= config.AI_INITIATIVE_DAILY:
        return False
    if not ai._ready(chat_id) or not ai.available():
        return False
    return not (ai.capped() and await ai.spent_today(chat_id) >= s.ai_daily)


async def seen(bot, s, chat_id: int, who: str) -> bool:
    """Ступень 1: человек появился в чате — спросить про его созревший план."""
    if not _on(s, 1):
        return False
    from . import ai, history as store
    try:
        ripe = await store.plans_ripe(chat_id, int(time.time()), who=who)
        if not ripe or not await _allowed(s, chat_id):
            return False
        return await _send(bot, s, chat_id, ripe[0], await ai.history(chat_id, config.AI_HISTORY))
    except Exception:
        logger.warning("не спросить %s про план в чате %s", who, chat_id, exc_info=True)
        return False


async def nudge(bot, s, chat_id: int, now: int) -> bool:
    """Ступень 2: человек не появился — написать самому, если чат живой и сейчас день."""
    if not _on(s, 2) or not _daytime(now):
        return False
    from . import ai, history as store
    ripe = await store.plans_ripe(chat_id, now, config.AI_INITIATIVE_WAIT)
    if not ripe or not await _allowed(s, chat_id):
        return False
    rows = await ai.history(chat_id, config.AI_HISTORY)
    people = [r.ts for r in rows if r.who != ai.SELF and r.ts]
    if not people or now - people[-1] > ACTIVE:
        return False                     # чат спит: писать в пустоту незачем
    if rows[-1].ts and now - rows[-1].ts < LULL:
        return False                     # идёт разговор — подождём паузы
    return await _send(bot, s, chat_id, ripe[0], rows)


async def tick(bot, now: int | None = None) -> None:
    """Один обход чатов: подготовить вопросы и, если пора, спросить самому."""
    from . import ai, memory
    now = now or int(time.time())
    if not ai.available():
        return
    for chat_id, _ in await memory._chats():
        try:
            s = await db.get_settings(chat_id)
            if not _on(s, 1):
                continue
            await prepare(chat_id, s, now)
            await nudge(bot, s, chat_id, now)
        except Exception:
            logger.warning("инициатива: чат %s сорвался", chat_id, exc_info=True)


async def loop(bot) -> None:
    """Фоновая задача на всё время работы бота."""
    await asyncio.sleep(90)
    while True:
        await tick(bot)
        await asyncio.sleep(EVERY)
