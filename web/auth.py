"""Кто открыл панель: проверка подписи Telegram и права на чат.

Мини-приложение отдаёт клиенту строку initData — набор полей, подписанных
токеном бота. Проверить её можно только зная токен, поэтому подделать
пользователя нельзя. Клиент шлёт эту строку в заголовке каждого запроса:
сессий и куки у нас нет, лишнее состояние ни к чему.
"""
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

from .. import config, db

logger = logging.getLogger("slusha.web.auth")


def check(init_data: str) -> dict | None:
    """Разобрать и проверить initData. Вернуть данные пользователя или None."""
    if not init_data or not config.BOT_TOKEN:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True,
                                       strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    got = data.pop("hash", None)
    if not got:
        return None

    # подпись считается по всем остальным полям, отсортированным по имени
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None

    # свежесть: старую подпись могли подсмотреть в логах прокси или туннеля
    try:
        age = time.time() - int(data.get("auth_date", 0))
    except ValueError:
        return None
    if age > config.WEB_INITDATA_TTL:
        return None

    try:
        user = json.loads(data.get("user") or "{}")
    except ValueError:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


async def allowed(user: dict) -> bool:
    """Пускать ли этого человека в панель — те же правила, что у меню бота."""
    uid = int(user["id"])
    if uid in config.ADMIN_IDS:
        return True
    return await db.access_allowed(uid, user.get("username"))
