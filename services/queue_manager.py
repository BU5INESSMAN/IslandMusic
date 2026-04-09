import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

from config import config
from services.downloader import TrackInfo, cleanup_file

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    chat_id: int
    track: TrackInfo
    caption: str


class QueueManager:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

    async def enqueue(self, item: QueueItem) -> None:
        await self._queue.put(item)

    async def enqueue_batch(self, items: list[QueueItem]) -> None:
        for item in items:
            await self._queue.put(item)

    async def _worker(self) -> None:
        batch_count = 0
        while True:
            item = await self._queue.get()
            try:
                await self._send_audio(item)
                batch_count += 1

                if batch_count >= config.max_files_per_batch:
                    batch_count = 0
                    logger.info(
                        "Batch limit reached, pausing for %d seconds",
                        config.batch_delay_seconds,
                    )
                    await asyncio.sleep(config.batch_delay_seconds)
            except Exception:
                logger.exception("Error processing queue item for chat %d", item.chat_id)
            finally:
                cleanup_file(item.track.filepath)
                self._queue.task_done()

    async def _send_audio(self, item: QueueItem) -> None:
        if not os.path.exists(item.track.filepath):
            logger.error("File not found: %s", item.track.filepath)
            return

        audio_file = FSInputFile(item.track.filepath)

        for attempt in range(5):
            try:
                await self._bot.send_audio(
                    chat_id=item.chat_id,
                    audio=audio_file,
                    title=item.track.title,
                    performer=item.track.artist,
                    duration=item.track.duration,
                    caption=item.caption,
                    parse_mode="HTML",
                )
                logger.info(
                    "Sent '%s' to chat %d", item.track.title, item.chat_id
                )
                return
            except TelegramRetryAfter as e:
                logger.warning(
                    "Rate limited, retrying after %d seconds", e.retry_after
                )
                await asyncio.sleep(e.retry_after)
            except Exception:
                logger.exception(
                    "Failed to send audio (attempt %d/5)", attempt + 1
                )
                if attempt == 4:
                    raise
                await asyncio.sleep(2)
