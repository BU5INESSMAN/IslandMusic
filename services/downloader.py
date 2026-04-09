from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import uuid
import zipfile
from dataclasses import dataclass

import aiohttp
import yt_dlp

from config import config
from services.progress import BatchProgress, BatchStatus

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com|"
    r"soundcloud\.com|open\.spotify\.com|deezer\.com|bandcamp\.com|"
    r"music\.yandex\.(?:ru|com|by|kz))"
)

_SPOTIFY_RE = re.compile(r"https?://(?:open\.)?spotify\.com/")
_YANDEX_RE = re.compile(r"https?://music\.yandex\.(?:ru|com|by|kz)/")
_DRM_DOMAINS = (_SPOTIFY_RE, _YANDEX_RE)

_OG_TITLE_RE = re.compile(
    r'<meta\s+(?:[^>]*?)property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)

_TITLE_JUNK = re.compile(
    r"\s*[\-\|–—]\s*(?:Spotify|Яндекс[\s.]?Музыка|Yandex[\s.]?Music|"
    r"Слушайте на|Listen on|Deezer).*$",
    re.IGNORECASE,
)

_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


@dataclass
class TrackInfo:
    filepath: str
    title: str
    artist: str
    album: str
    duration: int
    thumbnail: str | None = None


# ── URL helpers ───────────────────────────────────────────────────────

def is_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text))


def _is_drm_url(url: str) -> bool:
    return any(pat.search(url) for pat in _DRM_DOMAINS)


async def extract_metadata_from_url(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                if resp.status >= 400:
                    raise ValueError(f"HTTP {resp.status} fetching {url}")
                html = await resp.text(encoding="utf-8", errors="replace")
    except aiohttp.ClientError as exc:
        raise ValueError(f"Network error fetching {url}: {exc}") from exc

    match = _OG_TITLE_RE.search(html)
    if not match:
        match = _HTML_TITLE_RE.search(html)
    if not match:
        raise ValueError(f"Could not extract title from {url}")

    raw_title = match.group(1).strip()
    cleaned = _TITLE_JUNK.sub("", raw_title).strip()
    if not cleaned:
        raise ValueError(f"Empty title after cleanup from {url}")

    logger.info("Extracted metadata from URL %s -> '%s'", url, cleaned)
    return cleaned


# ── yt-dlp option builders ────────────────────────────────────────────

def _build_yt_dlp_opts(output_dir: str, filename_prefix: str) -> dict:
    return {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, f"{filename_prefix}_%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "embedthumbnail": True,
        "addmetadata": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }


def _build_search_opts(output_dir: str, filename_prefix: str) -> dict:
    opts = _build_yt_dlp_opts(output_dir, filename_prefix)
    opts["default_search"] = "ytsearch1"
    return opts


def _build_album_opts(output_dir: str, filename_prefix: str) -> dict:
    opts = _build_yt_dlp_opts(output_dir, filename_prefix)
    opts["noplaylist"] = False
    opts["outtmpl"] = os.path.join(
        output_dir, f"{filename_prefix}_%(playlist_index)02d_%(title)s.%(ext)s"
    )
    return opts


# ── Core sync helpers ─────────────────────────────────────────────────

def _extract_info(opts: dict, query: str) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
        return info or {}


def _find_downloaded_mp3(output_dir: str, prefix: str) -> list[str]:
    pattern = os.path.join(output_dir, f"{prefix}_*.mp3")
    return sorted(glob.glob(pattern))


def _parse_track_info(info: dict, filepath: str) -> TrackInfo:
    return TrackInfo(
        filepath=filepath,
        title=info.get("title") or info.get("track") or "Unknown Title",
        artist=info.get("artist") or info.get("uploader") or info.get("channel") or "Unknown Artist",
        album=info.get("album") or info.get("playlist_title") or "Single",
        duration=int(info.get("duration") or 0),
        thumbnail=info.get("thumbnail"),
    )


# ── Single-track download ────────────────────────────────────────────

async def download_track(query: str) -> TrackInfo:
    prefix = uuid.uuid4().hex[:8]
    output_dir = config.downloads_dir
    os.makedirs(output_dir, exist_ok=True)

    search_query = query

    if is_url(query) and _is_drm_url(query):
        logger.info("DRM/geo-blocked URL detected, extracting metadata: %s", query)
        extracted = await extract_metadata_from_url(query)
        opts = _build_search_opts(output_dir, prefix)
        search_query = f"{extracted} Official Audio"
    elif is_url(query):
        opts = _build_yt_dlp_opts(output_dir, prefix)
    else:
        opts = _build_search_opts(output_dir, prefix)
        search_query = f"{query} Official Audio"

    logger.info("Starting download: '%s'", search_query)
    info = await asyncio.to_thread(_extract_info, opts, search_query)

    files = _find_downloaded_mp3(output_dir, prefix)
    if not files:
        raise FileNotFoundError(f"No MP3 file found after downloading: {query}")

    track = _parse_track_info(info, files[0])
    logger.info("Finished download: '%s - %s'", track.artist, track.title)
    return track


# ── Batch download (with progress) ───────────────────────────────────

async def download_batch(
    queries: list[str],
    progress: BatchProgress,
) -> list[TrackInfo]:
    """Download a list of queries one-by-one, updating *progress* after each.

    Failed tracks are logged and skipped — the process continues.
    """
    downloaded: list[TrackInfo] = []

    for i, query in enumerate(queries, 1):
        progress.current_track = query
        logger.info(
            "Starting download for track [%d] of [%d]: '%s'",
            i, progress.total, query,
        )
        try:
            track = await download_track(query)
            downloaded.append(track)
            progress.done += 1
            logger.info(
                "Finished downloading/converting track [%d]: '%s - %s'",
                i, track.artist, track.title,
            )
        except Exception as exc:
            progress.failed += 1
            logger.error(
                "Failed to download track [%d] of [%d] ('%s'): %s",
                i, progress.total, query, exc,
            )

    progress.current_track = ""
    return downloaded


# ── Album download (with progress) ───────────────────────────────────

async def download_album(
    url: str,
    progress: BatchProgress | None = None,
) -> list[TrackInfo]:
    prefix = uuid.uuid4().hex[:8]
    output_dir = config.downloads_dir
    os.makedirs(output_dir, exist_ok=True)

    if _is_drm_url(url):
        raise yt_dlp.utils.DownloadError(
            "Album download from DRM-protected platforms is not supported. "
            "Please provide a YouTube/SoundCloud playlist link."
        )

    opts = _build_album_opts(output_dir, prefix)

    logger.info("Starting album download: %s", url)
    info = await asyncio.to_thread(_extract_info, opts, url)
    entries = info.get("entries") or []

    files = _find_downloaded_mp3(output_dir, prefix)
    if not files:
        raise FileNotFoundError(f"No MP3 files found after downloading album: {url}")

    tracks: list[TrackInfo] = []
    for i, filepath in enumerate(files):
        entry_info = entries[i] if i < len(entries) else info
        track = _parse_track_info(entry_info, filepath)
        tracks.append(track)
        if progress:
            progress.done += 1
            progress.current_track = f"{track.artist} - {track.title}"
            logger.info(
                "Finished downloading/converting track [%d] of [%d]: '%s - %s'",
                i + 1, progress.total, track.artist, track.title,
            )

    logger.info("Album download complete: %d tracks from %s", len(tracks), url)
    return tracks


async def get_album_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = await asyncio.to_thread(ydl.extract_info, url, download=False)
        return info or {}


# ── File cleanup ──────────────────────────────────────────────────────

def cleanup_file(filepath: str) -> None:
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info("Cleaned up: %s", filepath)
    except OSError as e:
        logger.warning("Failed to clean up %s: %s", filepath, e)


def cleanup_files(filepaths: list[str]) -> None:
    for fp in filepaths:
        cleanup_file(fp)


# ── ZIP archiving (with progress) ────────────────────────────────────

def _create_zip(
    tracks: list[TrackInfo],
    archive_name: str,
    progress: BatchProgress | None = None,
) -> str:
    archive_path = os.path.join(config.downloads_dir, archive_name)
    logger.info("Starting ZIP compression: %s (%d tracks)", archive_name, len(tracks))

    if progress:
        progress.status = BatchStatus.ARCHIVING
        progress.current_track = ""

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, track in enumerate(tracks, 1):
            if not os.path.exists(track.filepath):
                logger.warning("Skipping missing file: %s", track.filepath)
                continue
            ext = os.path.splitext(track.filepath)[1]
            arc_name = f"{i:02d} - {track.artist} - {track.title}{ext}"
            arc_name = _UNSAFE_CHARS_RE.sub("_", arc_name)
            zf.write(track.filepath, arc_name)

    size_mb = os.path.getsize(archive_path) / (1024 * 1024)
    logger.info(
        "Finished ZIP compression. File size: %.1fMB (%s)",
        size_mb, archive_path,
    )
    return archive_path


async def create_zip_archive(
    tracks: list[TrackInfo],
    archive_name: str,
    progress: BatchProgress | None = None,
) -> str:
    return await asyncio.to_thread(_create_zip, tracks, archive_name, progress)
