import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from app.config import settings

logger = logging.getLogger(__name__)

# Initialize Bot with default properties
bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# Initialize Dispatcher
dp = Dispatcher()
