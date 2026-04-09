from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message
from yt_dlp.utils import DownloadError, ExtractorError

from database.repository import log_download
from handlers.texts import (
    ALBUM_COMPLETE,
    ALBUM_FOUND,
    BATCH_ERROR,
    BATCH_START,
    CENSORED_ERROR_TEXT,
    DOWNLOAD_COMPLETE,
    DRM_ERROR_TEXT,
    ERROR_TEXT,
    FILE_TOO_LARGE,
    WAIT_TEXT,
    ZIP_COMPLETE,
    ZIP_PART_SENT,
    ZIP_SPLITTING,
    ZIP_THRESHOLD_NOTICE,
)
from services.admin import notify_admins
from services.downloader import (
    TELEGRAM_FILE_LIMIT_BYTES,
    CensoredTrackError,
    TrackInfo,
    cleanup_file,
    cleanup_files,
    create_zip_archives,
    download_album,
    download_batch,
    download_track,
    get_album_info,
    is_url,
)
from services.progress import BatchProgress, BatchStatus
from services.queue_manager import FileTooLargeError, QueueItem, QueueManager

logger = logging.getLogger(__name__)
router = Router()

ZIP_THRESHOLD = 10
PROGRESS_DEBOUNCE = 2  # minimum seconds between Telegram edits

_DRM_KEYWORDS = ("drm", "451", "geo", "unavailable for legal", "not available")
_COOKIE_ALERT_KEYWORDS = (
    "sign in to confirm your age",
    "cookies",
    "private video",
)
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _is_drm_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in _DRM_KEYWORDS)


def _get_queue_manager(bot: Bot) -> QueueManager:
    if not hasattr(bot, "_queue_manager"):
        bot._queue_manager = QueueManager(bot)  # type: ignore[attr-defined]
        bot._queue_manager.start()  # type: ignore[attr-defined]
    return bot._queue_manager  # type: ignore[attr-defined]


def _sanitize_filename(name: str) -> str:
    return _UNSAFE_FILENAME_RE.sub("_", name).strip("_ ")


def _schedule_admin_notification(bot: Bot, text: str) -> None:
    asyncio.create_task(notify_admins(bot, text))


def _schedule_error_notifications(bot: Bot, user_id: int, error_msg: str) -> None:
    safe_error = html.escape(error_msg)
    _schedule_admin_notification(
        bot,
        (
            "⚠️ <b>Ошибка загрузки:</b> "
            f"У пользователя {user_id} не скачался трек.\nОшибка: {safe_error}"
        ),
    )
    if any(keyword in error_msg.lower() for keyword in _COOKIE_ALERT_KEYWORDS):
        _schedule_admin_notification(
            bot,
            (
                "🚨 <b>КРИТИЧЕСКИЙ АЛЕРТ:</b> Скорее всего, файл cookies.txt "
                "просрочен! YouTube требует авторизацию (18+). "
                "Пожалуйста, обновите куки на сервере."
            ),
        )


# ── Progress updater (event-driven with debounce) ────────────────────

async def _run_progress_updater(
    status_msg: Message,
    progress: BatchProgress,
) -> None:
    """Edit the Telegram message immediately when progress changes,
    but no more often than every ``PROGRESS_DEBOUNCE`` seconds."""
    changed = asyncio.Event()
    progress.attach_event(changed)

    last_text = ""
    while not progress.finished:
        # Wait for a change signal OR timeout (fallback poll)
        try:
            await asyncio.wait_for(changed.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pass
        changed.clear()

        if progress.finished:
            break

        new_text = progress.format_message()
        if new_text != last_text:
            try:
                await status_msg.edit_text(new_text, parse_mode="HTML")
                last_text = new_text
            except Exception:
                logger.debug("Could not update progress message", exc_info=True)
            # Debounce: prevent editing faster than Telegram allows
            await asyncio.sleep(PROGRESS_DEBOUNCE)


# ── DB logging helper ─────────────────────────────────────────────────

async def _log_tracks(
    user_id: int,
    tracks: list[TrackInfo],
    query: str,
    source_url: str | None = None,
) -> None:
    """Log each successfully downloaded track to the database."""
    for track in tracks:
        await log_download(
            user_id=user_id,
            query=query,
            source_url=source_url,
            title=track.title,
            artist=track.artist,
            status="success",
        )


async def _log_failure(
    user_id: int,
    query: str,
    source_url: str | None = None,
) -> None:
    await log_download(
        user_id=user_id,
        query=query,
        source_url=source_url,
        status="failed",
    )


# ── Batch helpers ─────────────────────────────────────────────────────

async def _send_tracks_individually(
    tracks: list[TrackInfo], chat_id: int, bot: Bot,
) -> None:
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
    progress: BatchProgress,
    message: Message | None = None,
) -> None:
    """Archive tracks into one or more ZIPs and send each via Telegram."""
    safe_label = _sanitize_filename(archive_label) or "batch"
    archive_name = f"IslandMusic_{safe_label}.zip"

    track_paths = [t.filepath for t in tracks]
    zip_paths: list[str] = []

    try:
        zip_paths = await create_zip_archives(tracks, archive_name, progress)

        if len(zip_paths) > 1 and message:
            await message.answer(ZIP_SPLITTING, parse_mode="HTML")

        progress.status = BatchStatus.SENDING
        queue = _get_queue_manager(bot)

        for part_idx, zpath in enumerate(zip_paths, 1):
            size_mb = os.path.getsize(zpath) / (1024 * 1024)
            fname = os.path.basename(zpath)

            if len(zip_paths) == 1:
                caption = ZIP_COMPLETE.format(
                    filename=fname, count=len(tracks), size_mb=size_mb,
                )
            else:
                caption = ZIP_PART_SENT.format(
                    part=part_idx, total_parts=len(zip_paths), size_mb=size_mb,
                )

            try:
                await queue.send_document(chat_id, zpath, caption, progress)
            except FileTooLargeError:
                limit_mb = TELEGRAM_FILE_LIMIT_BYTES // (1024 * 1024)
                if message:
                    await message.answer(
                        FILE_TOO_LARGE.format(limit_mb=limit_mb),
                        parse_mode="HTML",
                    )
                return
    finally:
        cleanup_files(track_paths)
        cleanup_files(zip_paths)


async def _run_batch_with_progress(
    message: Message,
    bot: Bot,
    queries: list[str],
    archive_label: str,
    use_zip: bool,
) -> None:
    """Orchestrates a full batch download with a live progress message."""
    user_id = message.from_user.id
    progress = BatchProgress(total=len(queries))

    if use_zip:
        initial_text = ZIP_THRESHOLD_NOTICE + "\n\n" + BATCH_START
    else:
        initial_text = BATCH_START
    status_msg = await message.answer(initial_text, parse_mode="HTML")

    updater_task = asyncio.create_task(
        _run_progress_updater(status_msg, progress)
    )

    try:
        async def _on_track_error(failed_query: str, exc: Exception) -> None:
            await _log_failure(
                user_id,
                query=failed_query,
                source_url=failed_query if is_url(failed_query) else None,
            )
            _schedule_error_notifications(bot, user_id, str(exc))

        downloaded = await download_batch(queries, progress, on_error=_on_track_error)

        # Log results to DB
        if downloaded:
            await _log_tracks(user_id, downloaded, query="txt_batch")

        if not downloaded:
            progress.status = BatchStatus.FAILED
            progress.finished = True
            await updater_task
            await status_msg.edit_text(BATCH_ERROR, parse_mode="HTML")
            return

        if use_zip:
            await _send_tracks_as_zip(
                downloaded, message.chat.id, bot, archive_label, progress,
                message=message,
            )
        else:
            await _send_tracks_individually(downloaded, message.chat.id, bot)

        progress.status = BatchStatus.DONE
        progress.finished = True
        await updater_task

        final = progress.format_message()
        try:
            await status_msg.edit_text(final, parse_mode="HTML")
        except Exception:
            pass

    except Exception:
        logger.exception("Batch processing failed")
        progress.status = BatchStatus.FAILED
        progress.finished = True
        await updater_task
        try:
            await status_msg.edit_text(BATCH_ERROR, parse_mode="HTML")
        except Exception:
            pass


# ── .txt file handler ─────────────────────────────────────────────────

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
    date_label = datetime.now().strftime("%Y-%m-%d_%H-%M")
    _schedule_admin_notification(
        bot,
        (
            "📥 <b>Скачивание:</b> "
            f"Пользователь {message.from_user.id} запустил загрузку {len(lines)} треков."
        ),
    )

    await _run_batch_with_progress(
        message, bot, lines, date_label, use_zip,
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

        await log_download(
            user_id=user_id,
            query=text,
            source_url=text if is_url(text) else None,
            title=track.title,
            artist=track.artist,
            status="success",
        )

        await status_msg.delete()
    except CensoredTrackError as exc:
        logger.warning("Censored track for '%s': %s", text, exc)
        await _log_failure(user_id, query=text, source_url=text if is_url(text) else None)
        _schedule_error_notifications(bot, user_id, str(exc))
        await status_msg.edit_text(CENSORED_ERROR_TEXT, parse_mode="HTML")
    except (DownloadError, ExtractorError) as exc:
        logger.warning("yt-dlp error for '%s': %s", text, exc)
        await _log_failure(user_id, query=text, source_url=text if is_url(text) else None)
        _schedule_error_notifications(bot, user_id, str(exc))
        if _is_drm_error(exc):
            await status_msg.edit_text(DRM_ERROR_TEXT, parse_mode="HTML")
        else:
            await status_msg.edit_text(ERROR_TEXT, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Download failed for query: %s", text)
        await _log_failure(user_id, query=text)
        _schedule_error_notifications(bot, user_id, str(exc))
        await status_msg.edit_text(ERROR_TEXT, parse_mode="HTML")


# ── Album handler ─────────────────────────────────────────────────────

async def _handle_album_download(message: Message, bot: Bot, url: str) -> None:
    user_id = message.from_user.id

    try:
        album_info = await get_album_info(url)
        album_title = album_info.get("title", "Unknown Album")
        entries = album_info.get("entries") or []
        track_count = len(list(entries))

        use_zip = track_count > ZIP_THRESHOLD

        _schedule_admin_notification(
            bot,
            (
                "📥 <b>Скачивание:</b> "
                f"Пользователь {user_id} запустил загрузку {track_count} треков."
            ),
        )

        await message.answer(
            ALBUM_FOUND.format(title=album_title, count=track_count),
            parse_mode="HTML",
        )

        progress = BatchProgress(total=track_count)

        initial_text = BATCH_START
        if use_zip:
            initial_text = ZIP_THRESHOLD_NOTICE + "\n\n" + BATCH_START
        status_msg = await message.answer(initial_text, parse_mode="HTML")

        updater_task = asyncio.create_task(
            _run_progress_updater(status_msg, progress)
        )

        try:
            tracks = await download_album(url, progress)

            # Log all album tracks to DB
            await _log_tracks(user_id, tracks, query=album_title, source_url=url)

            if use_zip:
                await _send_tracks_as_zip(
                    tracks, message.chat.id, bot, album_title, progress,
                    message=message,
                )
            else:
                await _send_tracks_individually(tracks, message.chat.id, bot)

            progress.status = BatchStatus.DONE
            progress.finished = True
            await updater_task

            final = progress.format_message()
            try:
                await status_msg.edit_text(final, parse_mode="HTML")
            except Exception:
                pass

            await message.answer(
                ALBUM_COMPLETE.format(title=album_title, count=len(tracks)),
                parse_mode="HTML",
            )

        except Exception as exc:
            logger.exception("Album download/processing failed: %s", url)
            await _log_failure(user_id, query=album_title, source_url=url)
            _schedule_error_notifications(bot, user_id, str(exc))
            progress.status = BatchStatus.FAILED
            progress.finished = True
            await updater_task
            try:
                await status_msg.edit_text(BATCH_ERROR, parse_mode="HTML")
            except Exception:
                pass

    except CensoredTrackError as exc:
        logger.warning("Censored album for '%s': %s", url, exc)
        await _log_failure(user_id, query=url, source_url=url)
        _schedule_error_notifications(bot, user_id, str(exc))
        await message.answer(CENSORED_ERROR_TEXT, parse_mode="HTML")
    except (DownloadError, ExtractorError) as exc:
        logger.warning("yt-dlp album error for '%s': %s", url, exc)
        await _log_failure(user_id, query=url, source_url=url)
        _schedule_error_notifications(bot, user_id, str(exc))
        if _is_drm_error(exc):
            await message.answer(DRM_ERROR_TEXT, parse_mode="HTML")
        else:
            await message.answer(ERROR_TEXT, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Album download failed: %s", url)
        _schedule_error_notifications(bot, user_id, str(exc))
        await message.answer(ERROR_TEXT, parse_mode="HTML")
