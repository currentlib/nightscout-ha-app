import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from app.config import settings
from app.services.usage import current_telegram_id

logger = logging.getLogger(__name__)

class WhitelistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Resolve user ID from Message or CallbackQuery
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id

        if user_id is None:
            # Drop updates without user info
            logger.debug(f"Dropped event {type(event)} with no user info.")
            return

        # Restrict to whitelist user ID, unless public signup has been deliberately enabled
        # (see PUBLIC_SIGNUP_ENABLED in app/config.py) — in which case anyone gets in and
        # lands on a fresh "free" tier account instead of being blocked here.
        if not settings.PUBLIC_SIGNUP_ENABLED and user_id not in settings.TELEGRAM_WHITELIST_USER_IDS:
            logger.warning(
                f"Unauthorized access attempt from User ID: {user_id}. Whitelist: {settings.TELEGRAM_WHITELIST_USER_IDS}"
            )
            # Silently drop update to avoid spamming the logs/UI, or we could reply
            # If it's a message, we can choose to ignore or send a warning
            if isinstance(event, Message):
                await event.answer("❌ You are not authorized to use this bot.")
            return

        current_telegram_id.set(user_id)
        return await handler(event, data)
