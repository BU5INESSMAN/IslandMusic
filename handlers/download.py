import logging
import os
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message
from yt_dlp.utils import DownloadError, ExtractorError

from handlers.texts import (
    ALBUM_COMPLETE,
    ALBUM_FOUND,
    DOWNLOAD_COMPLETE,
    DRM_ERROR_TEXT,
    ERROR_TEXT,
    TXT_PARSING,
    WAIT_TEXT,
    ZIP_COMPLETE,
    ZIP_CREATING,
    ZIP_THRESHOLD_NOTICE,
)
from services.downloader import (
    TrackInfo,
    cleanup_file,
    cleanup_files,
    create_zip_archive,
    download_album,
    download_track,
    get_album_info,
    is_url,
)
from services.queue_manager import QueueItem, QueueManager

logger = logging.getLogger(__name__)
router = Router()

ZIP_THRESHOLD = 10

_DRM_KEYWORDS = ("drm", "451", "geo", "unavailable for legal", "not available")


def _is_drm_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in _DRM_KEYWORDS)


def _get_queue_manager(bot: Bot) -> QueueManager:
    if not hasattr(bot, "_queue_manager"):
        bot._queue_manager = QueueManager(bot)  # type: ignore[attr-defined]
        bot._queue_manager.start()  # type: ignore[attr-defined]
    return bot._queue_manager  # type: ignore[attr-defined]


def _sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe for filenames."""
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip("_ ")


async def _send_tracks_individually(
    tracks: list[TrackInfo], chat_id: int, bot: Bot
) -> None:
    """Enqueue tracks to be sent one-by-one as audio files."""
    queue = _get_queue_manager(bot)
    items = [
        QueueItem(
            chat_id=chat_id,
            track=track,
            caption=DOWNLOAD_COMPLETE.format(title=track.title, artist=track.artist),
        )
        for track in tracks
    ]
    await queue.enqueue_batch(items)


async def _send_tracks_as_zip(
    tracks: list[TrackInfo],
    chat_id: int,
    bot: Bot,
    archive_label: str,
    message: Message,
) -> None:
    """Compress tracks into a ZIP, send it as a document, then clean up everything."""
    safe_label = _sanitize_filename(archive_label) or "batch"
    archive_name = f"IslandMusic_{safe_label}.zip"

    status_msg = await message.answer(ZIP_CREATING, parse_mode="HTML")

    track_paths = [t.filepath for t in tracks]
    zip_path: str | None = None

    try:
        zip_path = await create_zip_archive(tracks, archive_name)

        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        caption = ZIP_COMPLETE.format(
            filename=archive_name, count=len(tracks), size_mb=size_mb
        )

        queue = _get_queue_manager(bot)
        await queue.send_document(chat_id, zip_path, caption)
        await status_msg.delete()
    except Exception:
        logger.exception("Failed to create/send ZIP archive")
        await status_msg.edit_text(ERROR_TEXT, parse_mode="HTML")
    finally:
        cleanup_files(track_paths)
        if zip_path:
            cleanup_file(zip_path)


# ── .txt file handler ────────────────────────────────────────────────

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

    use_zip = len(lines) > ZIP_THRESHOLD

    if use_zip:
        await message.answer(ZIP_THRESHOLD_NOTICE, parse_mode="HTML")
    else:
        await message.answer(
            TXT_PARSING.format(count=len(lines)), parse_mode="HTML"
        )

    downloaded: list[TrackInfo] = []
    failed_lines: list[str] = []

    for line in lines:
        try:
            track = await download_track(line)
            downloaded.append(track)
        except (DownloadError, ExtractorError) as exc:
            logger.warning("yt-dlp error for '%s': %s", line, exc)
            failed_lines.append(line)
            if _is_drm_error(exc):
                await message.answer(DRM_ERROR_TEXT, parse_mode="HTML")
            else:
                await message.answer(
                    f"⚠️ Не удалось загрузить: <code>{line}</code>",
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Unexpected error downloading: %s", line)
            failed_lines.append(line)
            await message.answer(
                f"⚠️ Не удалось загрузить: <code>{line}</code>",
                parse_mode="HTML",
            )

    if not downloaded:
        return

    if use_zip:
        date_label = datetime.now().strftime("%Y-%m-%d_%H-%M")
        await _send_tracks_as_zip(downloaded, message.chat.id, bot, date_label, message)
    else:
        await _send_tracks_individually(downloaded, message.chat.id, bot)

    await message.answer(
        f"✅ Загружено: <b>{len(downloaded)}/{len(lines)}</b> треков",
        parse_mode="HTML",
    )


# ── Text / URL handler ───────────────────────────────────────────────

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


# ── Album handler ─────────────────────────────────────────────────────

async def _handle_album_download(message: Message, bot: Bot, url: str) -> None:
    try:
        album_info = await get_album_info(url)
        album_title = album_info.get("title", "Unknown Album")
        entries = album_info.get("entries") or []
        track_count = len(list(entries))

        use_zip = track_count > ZIP_THRESHOLD

        if use_zip:
            await message.answer(
                ALBUM_FOUND.format(title=album_title, count=track_count)
                + "\n\n"
                + ZIP_THRESHOLD_NOTICE,
                parse_mode="HTML",
            )
        else:
            await message.answer(
                ALBUM_FOUND.format(title=album_title, count=track_count),
                parse_mode="HTML",
            )

        tracks = await download_album(url)

        if use_zip:
            await _send_tracks_as_zip(
                tracks, message.chat.id, bot, album_title, message
            )
        else:
            await _send_tracks_individually(tracks, message.chat.id, bot)

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
