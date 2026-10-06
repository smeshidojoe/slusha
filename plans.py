"""Незакрытые темы: что человек собирался сделать — чтобы потом спросить, как прошло.

«Завтра сдавать чертежи», «послезавтра забирать пойду», «купила бульдак,
завтра буду пробовать». Через пару дней человек пишет боту о другом, и живой
собеседник спросил бы: ну как чертежи? Бот без этого не спросит никогда —
реплика давно уехала из окна контекста, а в заметки такая мелочь не попадает.

Как устроено:
- Ловим словарём: срок («завтра», «в пятницу», «на выходных», «через
  неделю») и первое лицо в будущем («буду», «пойду», «сдавать», «у меня»).
  Вопросы и «до завтра» не в счёт. На трёх живых чатах — 4 срабатывания на
  3300 реплик, все четыре настоящие планы.
- Модель ничего не извлекает: в задание уходит сама реплика цитатой. Пересказ
  маленькой модели путал бы, кто что собирался и когда.
- Спрашиваем один раз и только в окне: с вечера назначенного дня и ещё
  четыре дня. Раньше — рано, позже — неуместно.
- Если человек сам уже рассказал, чем кончилось (его свежая реплика в
  переписке говорит о том же), тема закрыта и бот не переспрашивает.
"""
import logging
import re
import time

logger = logging.getLogger("slusha.plans")

# Часовой пояс чатов: «завтра» считаем по Москве, а не по UTC контейнера.
TZ = 3 * 3600
# С какого часа назначенного дня уже можно спрашивать.
ASK_HOUR = 17
# Сколько дней после этого вопрос ещё уместен.
ASK_DAYS = 4
# Сколько открытых тем держим на человека.
PER_PERSON = 3

_WHEN = re.compile(r"""(?ix)
 (?<!до\s)\b(?P<rel>послезавтра|завтра)\b
 | \bсегодня\s+(?P<today>вечером|ночью|днём|днем)\b
 | \bв\s+(?P<wd>понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b
 | \bна\s+(этих\s+|следующих\s+)?(?P<wknd>выходных)\b
 | \bна\s+(?P<nextwk>следующей\s+неделе)\b
 | \bчерез\s+(?P<in>неделю|пару\s+дней|два\s+дня|три\s+дня|\d+\s+дн\w+)\b
""")
# Первое лицо, будущее или «у меня …»: план говорящего, а не новость о мире.
_MINE = re.compile(r"""(?ix)
 \b(буду|будем|пойду|пойдём|пойдем|поеду|поедем|полечу|иду|идём|идем|еду|едем|лечу|летим
   |сдаю|сдавать|сдам|сдаём|забираю|забирать|встречаюсь|собираюсь|планирую|переезжаю|уезжаю
   |улетаю|начинаю|выхожу|попробую|приготовлю|куплю|закажу|сделаю|доделаю|допишу)\b
 | \bу\s+меня\b | \bмне\s+(надо|нужно|предстоит)\b
""")
_WEEKDAYS = ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу",
             "воскресенье")


def _days_ahead(m: re.Match, weekday: int) -> int:
    """Через сколько дней назначенное: 0 — сегодня."""
    if m.group("rel"):
        return 2 if m.group("rel").lower() == "послезавтра" else 1
    if m.group("today"):
        return 0
    if m.group("wd"):
        return (_WEEKDAYS.index(m.group("wd").lower()) - weekday) % 7
    if m.group("wknd"):
        return max(0, 5 - weekday)          # суббота; в воскресенье — сегодня
    if m.group("nextwk"):
        return 7 - weekday                  # понедельник следующей недели
    span = m.group("in").lower()
    if span.startswith("недел"):
        return 7
    if span.startswith(("пару", "два")):
        return 2
    if span.startswith("три"):
        return 3
    return int(re.match(r"\d+", span).group())


def detect(text: str, ts: int) -> tuple[int, int] | None:
    """План ли это. Да — (с какого момента спрашивать, до какого), unix-время."""
    t = (text or "").strip()
    if not (8 <= len(t) <= 300) or t.endswith("?"):
        return None
    m = _WHEN.search(t)
    if not m or not _MINE.search(t):
        return None
    local = ts + TZ
    day0 = local - local % 86400                       # полночь по Москве
    weekday = (day0 // 86400 + 3) % 7                  # 1970-01-01 — четверг
    ahead = _days_ahead(m, weekday)
    if m.group("today"):
        start = day0 + 86400 + 8 * 3600                # «сегодня вечером» — наутро
    else:
        start = day0 + ahead * 86400 + ASK_HOUR * 3600
    return start - TZ, start - TZ + ASK_DAYS * 86400


async def note(chat_id: int, who: str, text: str) -> None:
    """Запомнить реплику, если это план со сроком."""
    from . import history as store
    ts = int(time.time())
    when = detect(text, ts)
    if not when:
        return
    try:
        await store.plan_add(chat_id, who, text.strip(), ts, *when, PER_PERSON)
        logger.info("план %s в чате %s: %r", who, chat_id, text[:80])
    except Exception:
        logger.warning("не запомнить план %s в чате %s", who, chat_id, exc_info=True)


def _ago(ts: int, now: int) -> str:
    days = (now + TZ) // 86400 - (ts + TZ) // 86400
    if days <= 1:
        return "Вчера"
    if days == 2:
        return "Позавчера"
    return f"{days} дня назад" if days < 5 else f"{days} дней назад"


async def line(chat_id: int, who: str, rows=()) -> str:
    """Строка для задания про созревший план человека. Пусто — спрашивать не о чем.

    Отдаём одну тему и сразу помечаем её спрошенной: второй раз бот о ней
    напоминать не будет, даже если в этот раз промолчал.
    """
    from . import history as store, summary
    now = int(time.time())
    try:
        due = await store.plans_due(chat_id, who, now)
    except Exception:
        logger.warning("не прочитать планы %s в чате %s", who, chat_id, exc_info=True)
        return ""
    for pid, text, ts in due:
        await store.plan_done(pid)
        # Уже рассказал сам: его свежая реплика о том же — переспрашивать глупо.
        topic = summary._stems(text)
        told = any(ln.who == who and getattr(ln, "ts", 0) > ts and ln.text != text
                   and topic & summary._stems(ln.text) for ln in rows)
        if told:
            continue
        return (f"{_ago(ts, now)} {who} писал: «{text[:200]}». "
                "Если к месту — спроси, как прошло.")
    return ""
