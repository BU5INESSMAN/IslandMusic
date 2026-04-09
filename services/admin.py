from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ParseMode

from config import config

logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.warning(
                "Failed to send admin notification to %d",
                admin_id,
                exc_info=True,
            )
