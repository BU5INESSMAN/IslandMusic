import asyncio
import glob
import logging
import os
import re
import uuid
from dataclasses import dataclass

import yt_dlp

from config import config

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com|"
    r"soundcloud\.com|open\.spotify\.com|deezer\.com|bandcamp\.com)"
)


@dataclass
class TrackInfo:
    filepath: str
    title: str
    artist: str
    album: str
    duration: int
    thumbnail: str | None = None


def is_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text))


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


def _extract_info(opts: dict, query: str) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
        return info or {}


def _find_downloaded_mp3(output_dir: str, prefix: str) -> list[str]:
    pattern = os.path.join(output_dir, f"{prefix}_*.mp3")
    files = sorted(glob.glob(pattern))
    return files


def _parse_track_info(info: dict, filepath: str) -> TrackInfo:
    return TrackInfo(
        filepath=filepath,
        title=info.get("title") or info.get("track") or "Unknown Title",
        artist=info.get("artist") or info.get("uploader") or info.get("channel") or "Unknown Artist",
        album=info.get("album") or info.get("playlist_title") or "Single",
        duration=int(info.get("duration") or 0),
        thumbnail=info.get("thumbnail"),
    )


async def download_track(query: str) -> TrackInfo:
    prefix = uuid.uuid4().hex[:8]
    output_dir = config.downloads_dir
    os.makedirs(output_dir, exist_ok=True)

    if is_url(query):
        opts = _build_yt_dlp_opts(output_dir, prefix)
        search_query = query
    else:
        opts = _build_search_opts(output_dir, prefix)
        search_query = f"{query} Official Audio"

    info = await asyncio.to_thread(_extract_info, opts, search_query)

    files = _find_downloaded_mp3(output_dir, prefix)
    if not files:
        raise FileNotFoundError(f"No MP3 file found after downloading: {query}")

    return _parse_track_info(info, files[0])


async def download_album(url: str) -> list[TrackInfo]:
    prefix = uuid.uuid4().hex[:8]
    output_dir = config.downloads_dir
    os.makedirs(output_dir, exist_ok=True)

    opts = _build_album_opts(output_dir, prefix)

    info = await asyncio.to_thread(_extract_info, opts, url)
    entries = info.get("entries") or []

    files = _find_downloaded_mp3(output_dir, prefix)
    if not files:
        raise FileNotFoundError(f"No MP3 files found after downloading album: {url}")

    tracks: list[TrackInfo] = []
    for i, filepath in enumerate(files):
        entry_info = entries[i] if i < len(entries) else info
        tracks.append(_parse_track_info(entry_info, filepath))

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


def cleanup_file(filepath: str) -> None:
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info("Cleaned up: %s", filepath)
    except OSError as e:
        logger.warning("Failed to clean up %s: %s", filepath, e)
