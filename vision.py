"""Картинки для модели: скачать из Telegram и превратить в base64.

Отдельный тумблер в настройках чата, потому что зрение есть далеко не у всех
моделей: gemma3 и claude картинку разберут, а qwen3 или llama3 в лучшем случае
промолчат, в худшем — ответят ошибкой на весь запрос, и чат останется вообще
без ответа.

Берём только самый крупный размер фото: Telegram отдаёт лестницу превью, и
мелкие модели всё равно ничего не разглядят. Больше двух картинок за раз не
шлём — это само сообщение и то, на которое отвечают; всё остальное к поводу
ответить отношения не имеет.

Стикер — тоже картинка. Раньше модель знала о нём только эмодзи, которое
выбрал автор набора, а это часто мимо: мем с подписью приходил как «😂».
Обычный стикер — webp 512px, Ollama берёт его как есть. Анимированный (.tgs)
и видеостикер (.webm) модель не прочтёт, у них берём превью — первый кадр
128px. На пробе из 34 стикеров gemma3 узнавала по превью и персонажа, и
настроение почти так же, как по полной картинке.

Стикер, которым ответили самому боту, сюда не попадает: его описывает
ai.sticker_seen, и модель получает описание словами.
"""
import asyncio
import base64
import logging

from . import config

logger = logging.getLogger("slusha.vision")

_TRIES = 3


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


def _sticker(message) -> object | None:
    """Чем показать стикер модели. None — стикера нет или показать нечем."""
    sticker = getattr(message, "sticker", None)
    if sticker is None:
        return None
    if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
        return getattr(sticker, "thumbnail", None)
    return sticker


def has_sticker(message) -> bool:
    return _sticker(message) is not None


async def sticker(bot, message) -> str | None:
    """Картинка стикера в base64. None — показать нечем или не скачалась."""
    size = _sticker(message)
    return await _one(bot, size) if size is not None else None


async def _one(bot, size) -> str | None:
    """Скачать и закодировать один размер. None — не влезло или не скачалось."""
    limit = config.AI_IMAGE_MAX_BYTES
    if (getattr(size, "file_size", None) or 0) > limit:
        logger.info("картинка %s байт больше лимита %s, пропускаю",
                    size.file_size, limit)
        return None
    # Соединение с Telegram рвётся на ровном месте, особенно сразу после
    # запуска, пока сеть не поднялась: ConnectionResetError посреди TLS. Со
    # второй-третьей попытки обычно проходит.
    raw = None
    for attempt in range(_TRIES):
        try:
            raw = (await bot.download(size.file_id)).read()
            break
        except Exception as e:
            if attempt + 1 == _TRIES:
                logger.warning("не скачать картинку: %s", e)
                return None
            await asyncio.sleep(attempt + 1)
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
        if size is None and src is not message:
            size = _sticker(src)         # стикер, на который отвечают
        if size is None:
            continue
        data = await _one(bot, size)
        if data:
            out.append(data)
    return out
