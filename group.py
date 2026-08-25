"""Групповые чаты: регистрация бота и сами разговоры.

Модерации тут нет вовсе. Всё, что делает бот в группе, — запоминает реплики и
иногда отвечает. Ни удалять, ни наказывать он не умеет, и прав ему для этого
не нужно: хватает обычного участника с выключенным privacy mode.
"""
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import (IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter)
from aiogram.types import ChatMemberUpdated, Message, MessageReactionUpdated

from . import ai, config, db, history as store, reactions

logger = logging.getLogger("slusha.group")

router = Router()


def stale(event) -> bool:
    """Сообщение из бэклога: после простоя отвечать на вчерашнее незачем."""
    ts = getattr(event, "date", None)
    return bool(ts and time.time() - ts.timestamp() > config.MSG_MAX_AGE)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(update: ChatMemberUpdated, bot: Bot) -> None:
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    adder = update.from_user
    owner_id = adder.id if adder and not adder.is_bot else None

    # бот работает только там, куда его позвал кто-то из допущенных
    allowed = owner_id is not None and (
        owner_id in config.ADMIN_IDS
        or await db.access_allowed(owner_id, adder.username if adder else None)
    )
    if not allowed:
        try:
            await bot.leave_chat(chat.id)
        except Exception:
            logger.warning("не вышел из чужого чата %s", chat.id, exc_info=True)
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Бота добавили в чужой чат «{chat.title}» "
                    f"(<code>{chat.id}</code>), добавил <code>{owner_id}</code>. "
                    f"Бот вышел.")
            except Exception:
                pass
        return

    await db.upsert_chat(chat.id, chat.title, chat.username, owner_id)
    try:
        await bot.send_message(
            owner_id,
            f"✅ Бот добавлен в чат <b>{chat.title}</b>.\n"
            f"Открой /menu и включи разум — по умолчанию он молчит.")
    except Exception:
        pass          # ещё не писал боту в личку


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed(update: ChatMemberUpdated, bot: Bot) -> None:
    if update.chat.type in ("group", "supergroup"):
        await db.set_chat_active(update.chat.id, False)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_message(message: Message, bot: Bot) -> None:
    """Единственная точка входа группы: запомнить реплику и, может, ответить."""
    if stale(message):
        return
    user = message.from_user
    if user is None or user.id in config.SERVICE_IDS:
        return

    ch = await db.get_chat(message.chat.id)
    if ch is None:
        return                     # чат не зарегистрирован — нас сюда не звали
    if ch["title"] != message.chat.title:
        await db.update_chat_title(message.chat.id, message.chat.title,
                                   message.chat.username)
    s = await db.get_settings(message.chat.id)
    await ai.maybe_reply(bot, message, s)


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated) -> None:
    """Реакция под сообщением — тоже реплика, дописываем её в историю.

    Апдейт message_reaction приходит боту, только если он админ чата: обычному
    участнику Telegram его не отдаёт вовсе, молча. Прав для этого никаких не
    нужно — достаточно самого звания, и без него всё остальное работает
    по-прежнему, просто реакций бот не видит.

    Хендлер обязан быть зарегистрирован до старта поллинга: список апдейтов
    собирает resolve_used_update_types, и без хендлера message_reaction в него
    не попадёт.
    """
    if event.chat.type not in ("group", "supergroup"):
        return
    if await db.get_chat(event.chat.id) is None:
        return                     # чат не зарегистрирован — нас сюда не звали
    change = reactions.delta(event.old_reaction, event.new_reaction)
    if not change:
        return
    try:
        current = await store.reactions_of(event.chat.id, event.message_id)
        await ai.set_reactions(event.chat.id, event.message_id,
                               reactions.merge(current, change))
    except Exception:
        logger.warning("не записать реакцию в чате %s", event.chat.id, exc_info=True)
