"""Мелочи, нужные и меню, и модели."""
import html
import time
from datetime import datetime, timedelta, timezone

from . import config


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _tz() -> timezone:
    return timezone(timedelta(hours=config.TZ_OFFSET))


def day_num(ts: float | None = None) -> int:
    """Номер местных суток — по нему считается дневной лимит ответов."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), _tz())
    return dt.toordinal()


def stamp(ts: int | float) -> str:
    """Метка времени для меню и панели, в часовом поясе бота."""
    return datetime.fromtimestamp(ts, _tz()).strftime("%d.%m %H:%M")


def msg_gone(e: Exception) -> bool:
    """Ошибка «сообщения больше нет» — на такое ругаться в лог не стоит."""
    text = str(e).lower()
    return "message to be replied not found" in text or "message to reply not found" in text \
        or "message can't be deleted" in text or "message to delete not found" in text


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many
