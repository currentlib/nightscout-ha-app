import asyncio
import logging

from app.bootstrap import prepare

# Before anything reads app.config: turns the HA options into settings and gives up root.
prepare()

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import BotCommand, ErrorEvent

from app.bot.bot import bot, dp
from app.bot.handlers import check_and_send_reminders, router as bot_router
from app.bot.middleware import WhitelistMiddleware
from app.database import init_db
from app.services.backup import backup_database
from app.services.nightscout_sync import sync_cycle
from app.services.scheduler import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

SYNC_SECONDS = 30
RETRY_SECONDS = 30
COMMANDS = [
    BotCommand(command="status", description="🔍 Перевірити статус (Цукор та IOB)"),
    BotCommand(command="baselines", description="📊 Інструкція з показників (ICR, ISF)"),
    BotCommand(command="analyze", description="📈 Аналіз чутливості та трендів (7 днів)"),
    BotCommand(command="reminders", description="⏰ Налаштування нагадувань"),
    BotCommand(command="insulin", description="💉 Записати інсулін без їжі"),
    BotCommand(command="clear_chat", description="🧹 Очистити історію діалогу з ШІ"),
]

# Register Whitelist Middleware & Router for the Bot
dp.message.middleware(WhitelistMiddleware())
dp.callback_query.middleware(WhitelistMiddleware())
dp.include_router(bot_router)


# Global safety net: any handler can hit TelegramRetryAfter (flood control) on a send/edit it
# didn't individually guard against — a severely flood-limited chat can stay blocked for hours,
# and without this every such call surfaces as a full unhandled-exception traceback instead of
# a one-line log. This doesn't make the send succeed; it just stops a known, expected failure
# mode from looking like a crash. Registered once here so no individual handler needs its own.
@dp.error(ExceptionTypeFilter(TelegramRetryAfter))
async def handle_flood_control(event: ErrorEvent):
    logger.warning(
        f"Flood control hit, update dropped (retry after {event.exception.retry_after}s): "
        f"update_id={event.update.update_id}"
    )


async def main():
    logger.info("Initializing database...")
    await init_db()

    try:
        await bot.set_my_commands(COMMANDS)
    except TelegramNetworkError as error:
        logger.warning(f"Could not set the command menu: {error}")


    scheduler.add_job(check_and_send_reminders, 'interval', minutes=15, id="check_and_send_reminders", replace_existing=True)
    scheduler.add_job(backup_database, 'cron', hour=3, minute=0, id="backup_database", replace_existing=True)
    scheduler.add_job(sync_cycle, 'interval', seconds=SYNC_SECONDS, id="nightscout_sync", max_instances=1, replace_existing=True)
    scheduler.start()

    logger.info("Starting Telegram Bot (polling mode)...")
    try:
        # After a power cut the Pi is often up before the internet is. start_polling returns
        # on SIGTERM, which is how the app stops, and raises if Telegram can't be reached yet.
        while True:
            try:
                await dp.start_polling(bot)
                break
            except TelegramNetworkError as error:
                logger.warning(f"Telegram is unreachable, retrying in {RETRY_SECONDS}s: {error}")
                await asyncio.sleep(RETRY_SECONDS)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
