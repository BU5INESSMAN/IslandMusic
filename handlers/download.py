import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from yt_dlp.utils import DownloadError, ExtractorError

from handlers.texts import (
    ALBUM_COMPLETE,
    ALBUM_FOUND,
    DOWNLOAD_COMPLETE,
    DRM_ERROR_TEXT,
    ERROR_TEXT,
    FALLBACK_SEARCH_TEXT,
    TXT_PARSING,
    WAIT_TEXT,
)
from services.downloader import (
    cleanup_file,
    download_album,
    download_track,
    get_album_info,
    is_url,
)
from services.queue_manager import QueueItem, QueueManager

logger = logging.getLogger(__name__)
router = Router()

_DRM_KEYWORDS = ("drm", "451", "geo", "unavailable for legal", "not available")


def _is_drm_error(exc: Exception) -> bool:
    """Check if a yt-dlp exception is DRM / geo-block related."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _DRM_KEYWORDS)


def _get_queue_manager(bot: Bot) -> QueueManager:
    if not hasattr(bot, "_queue_manager"):
        bot._queue_manager = QueueManager(bot)  # type: ignore[attr-defined]
        bot._queue_manager.start()  # type: ignore[attr-defined]
    return bot._queue_manager  # type: ignore[attr-defined]


@router.message(F.document)
async def handle_txt_file(message: Message, bot: Bot) -> None:
    doc = message.document
    if not doc.file_name or not doc.file_name.endswith(".txt"):
        return

    logger.info("User %d uploaded txt file: %s", message.from_user.id, doc.file_name)

    file = await bot.download(doc)
    if file is None:
        await message.answer(ERROR_TEXT, parse_mode="HTML")
        return

    content = file.read().decode("utf-8", errors="ignore")
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    if not lines:
        await message.answer("📁 Файл пуст или не содержит треков.", parse_mode="HTML")
        return

    await message.answer(
        TXT_PARSING.format(count=len(lines)), parse_mode="HTML"
    )

    queue = _get_queue_manager(bot)
    success_count = 0

    for line in lines:
        try:
            track = await download_track(line)
            caption = DOWNLOAD_COMPLETE.format(title=track.title, artist=track.artist)
            await queue.enqueue(
                QueueItem(chat_id=message.chat.id, track=track, caption=caption)
            )
            success_count += 1
        except (DownloadError, ExtractorError) as exc:
            logger.warning("yt-dlp error for '%s': %s", line, exc)
            if _is_drm_error(exc):
                await message.answer(DRM_ERROR_TEXT, parse_mode="HTML")
            else:
                await message.answer(
                    f"⚠️ Не удалось загрузить: <code>{line}</code>",
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Unexpected error downloading from txt: %s", line)
            await message.answer(
                f"⚠️ Не удалось загрузить: <code>{line}</code>",
                parse_mode="HTML",
            )

    if success_count > 0:
        await message.answer(
            f"✅ Поставлено в очередь: <b>{success_count}/{len(lines)}</b> треков",
            parse_mode="HTML",
        )


@router.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    text = message.text.strip()

    if text.startswith("/"):
        return

    if text in ("🎵 Поиск трека", "💿 Скачать альбом", "📁 Список из .txt", "ℹ️ О IslandMusic"):
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    if is_url(text) and any(
        kw in text for kw in ["/playlist", "/album", "/sets/", "list="]
    ):
        logger.info("User %d requested album: %s", user_id, text)
        await _handle_album_download(message, bot, text)
        return

    logger.info("User %d searching: %s", user_id, text)
    status_msg = await message.answer(WAIT_TEXT, parse_mode="HTML")

    try:
        track = await download_track(text)
        caption = DOWNLOAD_COMPLETE.format(title=track.title, artist=track.artist)

        queue = _get_queue_manager(bot)
        await queue.enqueue(
            QueueItem(chat_id=chat_id, track=track, caption=caption)
        )

        await status_msg.delete()
    except (DownloadError, ExtractorError) as exc:
        logger.warning("yt-dlp error for '%s': %s", text, exc)
        if _is_drm_error(exc):
            await status_msg.edit_text(DRM_ERROR_TEXT, parse_mode="HTML")
        else:
            await status_msg.edit_text(ERROR_TEXT, parse_mode="HTML")
    except Exception:
        logger.exception("Download failed for query: %s", text)
        await status_msg.edit_text(ERROR_TEXT, parse_mode="HTML")


async def _handle_album_download(message: Message, bot: Bot, url: str) -> None:
    try:
        album_info = await get_album_info(url)
        album_title = album_info.get("title", "Unknown Album")
        entries = album_info.get("entries") or []
        track_count = len(list(entries))

        await message.answer(
            ALBUM_FOUND.format(title=album_title, count=track_count),
            parse_mode="HTML",
        )

        tracks = await download_album(url)
        queue = _get_queue_manager(bot)

        items: list[QueueItem] = []
        for track in tracks:
            caption = DOWNLOAD_COMPLETE.format(title=track.title, artist=track.artist)
            items.append(QueueItem(chat_id=message.chat.id, track=track, caption=caption))

        await queue.enqueue_batch(items)

        await message.answer(
            ALBUM_COMPLETE.format(title=album_title, count=len(tracks)),
            parse_mode="HTML",
        )
    except (DownloadError, ExtractorError) as exc:
        logger.warning("yt-dlp album error for '%s': %s", url, exc)
        if _is_drm_error(exc):
            await message.answer(DRM_ERROR_TEXT, parse_mode="HTML")
        else:
            await message.answer(ERROR_TEXT, parse_mode="HTML")
    except Exception:
        logger.exception("Album download failed: %s", url)
        await message.answer(ERROR_TEXT, parse_mode="HTML")
