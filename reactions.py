"""Реакции как часть разговора.

Смайлик под сообщением — это реплика: «👍×5» под чьей-то шуткой говорит о
чате больше, чем следующие три сообщения. Модель их не видит вовсе, поэтому
дописываем их в историю строкой и рендерим в промпте как [реакции: 👍×2, 🔥].

Как считаем. Апдейт message_reaction приходит на каждое изменение и приносит
old_reaction и new_reaction — полные списки реакций ОДНОГО человека до и после.
Общего счётчика в нём нет, поэтому ведём его сами: чего в новом списке не было
в старом — плюс один, чего не стало — минус один. Так хватает одной строки в
базе, без таблицы «кто что поставил».

Важное про доступ: message_reaction приходит боту, только если он админ чата.
Обычному участнику Telegram эти апдейты не отдаёт вовсе — молча, без ошибки.
"""
import logging
import re

logger = logging.getLogger("slusha.reactions")

# Кастомные эмодзи боту не отдаются: в апдейте лежит только id набора, самой
# картинки нет. Показываем значком — «тут что-то поставили» честнее, чем
# промолчать или соврать конкретным смайликом.
CUSTOM = "✱"
PAID = "⭐"
# сколько разных реакций показываем в промпте: дальше идёт длинный хвост
# одиночных, который занимает место и ничего не добавляет
TOP = 6

_ITEM = re.compile(r"^(.+?)(?:×(\d+))?$")


def parse(text: str | None) -> dict[str, int]:
    """Строку «👍×2, 🔥» — обратно в счётчик."""
    counts: dict[str, int] = {}
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _ITEM.match(chunk)
        if not m:
            continue
        counts[m.group(1)] = int(m.group(2) or 1)
    return counts


def render(counts: dict[str, int]) -> str:
    """Счётчик — в строку для истории и промпта. Пусто — реакций не осталось."""
    live = [(e, n) for e, n in counts.items() if n > 0]
    live.sort(key=lambda x: (-x[1], x[0]))
    return ", ".join(e if n == 1 else f"{e}×{n}" for e, n in live[:TOP])


def _label(reaction) -> str:
    """Как назвать одну реакцию из апдейта."""
    kind = getattr(reaction, "type", "")
    if kind == "emoji":
        return getattr(reaction, "emoji", "") or CUSTOM
    if kind == "paid":
        return PAID
    return CUSTOM


def delta(old: list | None, new: list | None) -> dict[str, int]:
    """На сколько изменился счётчик после действий одного человека."""
    was = [_label(r) for r in (old or [])]
    now = [_label(r) for r in (new or [])]
    out: dict[str, int] = {}
    for label in now:
        if label in was:
            was.remove(label)           # не менялась — не трогаем счётчик
            continue
        out[label] = out.get(label, 0) + 1
    for label in was:
        out[label] = out.get(label, 0) - 1
    return out


def merge(current: str | None, change: dict[str, int]) -> str:
    """Применить изменение к строке реакций сообщения."""
    counts = parse(current)
    for label, d in change.items():
        counts[label] = max(0, counts.get(label, 0) + d)
    return render(counts)
