"""Сборка и запуск бота-собеседника."""
import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, ErrorEvent, Message, MenuButtonCommands

from . import ai, config, db, group, history, menu

logger = logging.getLogger("slusha")

_DENIED = ("🔒 Доступ к боту закрыт.\n\n"
           "Бот работает по списку допущенных. Обратитесь к владельцу за доступом.")


class AccessMiddleware(BaseMiddleware):
    """Личка: пишем юзера в базу и отсекаем не допущенных."""

    async def __call__(self, handler, event, data):
        user, private = None, False
        if isinstance(event, Message):
            user = event.from_user
            private = event.chat.type == "private"
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            private = event.message is not None and event.message.chat.type == "private"
        if user is not None and private and not user.is_bot:
            if user.id not in config.ADMIN_IDS and \
                    not await db.access_allowed(user.id, user.username):
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ к боту закрыт.", show_alert=True)
                else:
                    await event.answer(_DENIED)
                return None
            await db.track_user(user.id, user.username, user.first_name)
        return await handler(event, data)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.LOG_PATH, encoding="utf-8")],
    )


async def _on_error(event: ErrorEvent) -> None:
    logger.exception("update error", exc_info=event.exception)


async def main() -> None:
    _setup_logging()
    if not config.BOT_TOKEN:
        raise RuntimeError("SLUSHA_BOT_TOKEN is not set (slusha/.env)")
    if not ai.available():
        logger.warning("модель не настроена: бот будет молчать, пока не задан "
                       "AI_MODEL и ключ либо адрес")

    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher()

    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.errors.register(_on_error)

    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    # порядок важен: меню (личка) до группового catch-all
    dp.include_routers(menu.router, group.router)

    # панель поднимаем до поллинга: если порт занят, лучше упасть сразу,
    # чем узнать об этом через час по жалобе «панель не открывается»
    web_runner = None
    if config.WEB_ENABLED:
        from .web import server as web_server
        web_runner = await web_server.start(bot)

    try:
        # список апдейтов собирается по зарегистрированным хендлерам: реакции
        # (message_reaction) попадают в него только благодаря хендлеру в group
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                               drop_pending_updates=True)
    finally:
        if web_runner is not None:
            from .web import server as web_server
            await web_server.stop(web_runner)
        await history.close()
        await db.close()
        await bot.session.close()
        logger.info("slusha stopped")


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutdown by signal")
