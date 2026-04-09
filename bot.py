import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from database.engine import init_db
from handlers import main_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    logger.info("Initializing database...")
    await init_db()

    os.makedirs(config.downloads_dir, exist_ok=True)
    logger.info("IslandMusic bot started successfully")


async def main() -> None:
    if not config.bot_token:
        logger.error("BOT_TOKEN is not set. Check your .env file.")
        sys.exit(1)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(main_router)
    dp.startup.register(on_startup)

    logger.info("Starting IslandMusic bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
