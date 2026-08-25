"""Сервер панели: живёт в том же процессе, что и бот.

Отдельный процесс тут ни к чему — панели нужны та же база и тот же объект Bot,
чтобы писать в чаты и выходить из них ровно так же, как это делает меню.
aiohttp уже стоит как зависимость aiogram, новых пакетов не появляется.

Наружу порт выводит туннель (ngrok, cloudflared): Telegram открывает мини-
приложение только по https, а сертификат нам заводить незачем.
"""
import logging
import os

from aiohttp import web

from .. import config
from . import api, auth

logger = logging.getLogger("slusha.web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@web.middleware
async def _auth_mw(request: web.Request, handler):
    """Подпись Telegram на каждом запросе к API.

    Саму страницу отдаём кому угодно: без initData она всё равно ничего не
    покажет — данные приходят только через API.
    """
    if not request.path.startswith("/api/"):
        return await handler(request)

    user = auth.check(request.headers.get("X-Init-Data", ""))
    if user is None:
        return web.json_response({"error": "Нет подписи Telegram. Откройте панель "
                                           "кнопкой в боте."}, status=401)
    if not await auth.allowed(user):
        return web.json_response({"error": "Доступ к боту закрыт."}, status=403)
    request["user"] = user
    return await handler(request)


@web.middleware
async def _errors_mw(request: web.Request, handler):
    """Ошибки — в JSON: клиент показывает текст человеку, а не «500»."""
    try:
        return await handler(request)
    except web.HTTPException as e:
        if request.path.startswith("/api/"):
            return web.json_response({"error": e.text or e.reason}, status=e.status)
        raise
    except Exception:
        logger.exception("панель: запрос %s упал", request.path)
        return web.json_response({"error": "Внутренняя ошибка, смотрите логи."},
                                 status=500)


async def _index(request: web.Request) -> web.FileResponse:
    resp = web.FileResponse(os.path.join(STATIC, "index.html"))
    # WebView Telegram охотно кэширует страницу, и после обновления панели
    # у людей остаётся старая. Страница крошечная, перепроверять её каждый раз
    # дешевле, чем ловить потом «а у меня всё по-старому»
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def build(bot) -> web.Application:
    app = web.Application(middlewares=[_errors_mw, _auth_mw],
                          client_max_size=config.WEB_UPLOAD_MAX + 1024)
    app["bot"] = bot
    app.add_routes(api.routes)
    app.router.add_get("/", _index)
    app.router.add_static("/static/", STATIC, name="static")
    return app


async def start(bot):
    """Поднять сервер. Вернёт runner, который надо будет остановить."""
    runner = web.AppRunner(build(bot), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    logger.info("панель слушает %s:%s, публичный адрес %s",
                config.WEB_HOST, config.WEB_PORT, config.WEBAPP_URL or "не задан")
    return runner


async def stop(runner) -> None:
    if runner is not None:
        await runner.cleanup()
