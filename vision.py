"""Картинки для модели: скачать из Telegram и превратить в base64.

Отдельный тумблер в настройках чата, потому что зрение есть далеко не у всех
моделей: gemma3 и claude картинку разберут, а qwen3 или llama3 в лучшем случае
промолчат, в худшем — ответят ошибкой на весь запрос, и чат останется вообще
без ответа.

Берём только самый крупный размер фото: Telegram отдаёт лестницу превью, и
мелкие модели всё равно ничего не разглядят. Больше двух картинок за раз не
шлём — это само сообщение и то, на которое отвечают; всё остальное к поводу
ответить отношения не имеет.
"""
import base64
import logging

from . import config

logger = logging.getLogger("slusha.vision")


def _biggest(message) -> object | None:
    """Самый крупный размер фото сообщения. None — фото нет."""
    photo = getattr(message, "photo", None)
    if not photo:
        return None
    # Telegram присылает размеры по возрастанию, но полагаться на порядок,
    # который нигде не обещан, незачем
    return max(photo, key=lambda p: (getattr(p, "width", 0) or 0)
               * (getattr(p, "height", 0) or 0))


def has_photo(message) -> bool:
    return _biggest(message) is not None


async def _one(bot, size) -> str | None:
    """Скачать и закодировать один размер. None — не влезло или не скачалось."""
    limit = config.AI_IMAGE_MAX_BYTES
    if (getattr(size, "file_size", None) or 0) > limit:
        logger.info("картинка %s байт больше лимита %s, пропускаю",
                    size.file_size, limit)
        return None
    try:
        buf = await bot.download(size.file_id)
        raw = buf.read()
    except Exception:
        logger.warning("не скачать картинку", exc_info=True)
        return None
    if len(raw) > limit:
        # file_size у Telegram необязательное поле: бывает, что его нет вовсе,
        # и настоящий размер выясняется только после скачивания
        logger.info("картинка оказалась %s байт, пропускаю", len(raw))
        return None
    return base64.b64encode(raw).decode()


async def grab(bot, message) -> list[str]:
    """Картинки повода ответить: из самого сообщения и из того, на что отвечают."""
    out = []
    for src in (message, getattr(message, "reply_to_message", None)):
        if src is None or len(out) >= config.AI_IMAGE_MAX:
            continue
        size = _biggest(src)
        if size is None:
            continue
        data = await _one(bot, size)
        if data:
            out.append(data)
    return out
