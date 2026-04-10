from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp
import yt_dlp
from mutagen.id3 import COMM, TALB, TIT2, TPE1
from mutagen.mp3 import MP3

from config import config
from services.progress import BatchProgress, BatchStatus

logger = logging.getLogger(__name__)

# ── Censorship date cutoff ───────────────────────────────────────────
CENSORSHIP_DATE = "20260301"


class CensoredTrackError(Exception):
    """Raised when a track was uploaded after the censorship cutoff date."""

# ── Branding constants ────────────────────────────────────────────────
BRAND_ALBUM = "IslandMusic Bot (@island_music_bot)"
BRAND_COMMENT = "Downloaded via IslandMusic - your premium music assistant."

_README_CONTENT = """\
=== IslandMusic ===

Thank you for using IslandMusic Bot (@island_music_bot)!

Check out our other services:
  - IslandVPN  — fast, private, no-log VPN: @island_vpn_bot
  - IslandCloud — secure file hosting: @island_cloud_bot

Enjoy your music!
""".encode("utf-8")

# ── Title-cleaning patterns ───────────────────────────────────────────
# Suffixes commonly appended by YouTube uploaders
_TITLE_JUNK_SUFFIXES = re.compile(
    r"\s*[\(\[\|]?\s*"
    r"(?:Official\s*(?:Audio|Video|Music\s*Video|Lyric\s*Video|Visualizer|MV)|"
    r"Lyric(?:s)?\s*Video|High\s*Quality|HQ|HD|4K|Audio|"
    r"(?:Official\s*)?Clip|Original\s*(?:Audio|Mix)|Remastered|"
    r"Explicit|Clean\s*Version|Music\s*Video)"
    r"\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
# Brackets with platform noise: [Official Audio], (HQ), etc.
_BRACKET_JUNK = re.compile(
    r"\s*[\(\[]\s*"
    r"(?:Official(?:\s+\w+)?|Audio|Video|Lyrics?|HQ|HD|4K|"
    r"Explicit|Remastered|Original(?:\s+\w+)?|Clean(?:\s+\w+)?)"
    r"\s*[\)\]]",
    re.IGNORECASE,
)
# Hyphen-like separators used in "Artist - Title"
_ARTIST_TITLE_SEP = re.compile(r"\s+[-–—]\s+")

# Platform junk in og:title scraping
_TITLE_PLATFORM_JUNK = re.compile(
    r"\s*[\-\|–—]\s*(?:Spotify|Яндекс[\s.]?Музыка|Yandex[\s.]?Music|"
    r"Слушайте на|Listen on|Deezer).*$",
    re.IGNORECASE,
)

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

_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


@dataclass
class TrackInfo:
    filepath: str
    title: str
    artist: str
    album: str
    duration: int
    source: str = ""
    thumbnail: str | None = None


# ── Title / metadata cleaning ─────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """Strip 'Official Audio', '(HQ)', '[Lyrics]', etc. from a title."""
    cleaned = _BRACKET_JUNK.sub("", raw)
    cleaned = _TITLE_JUNK_SUFFIXES.sub("", cleaned)
    return cleaned.strip()


def _smart_split_artist_title(
    raw_title: str,
    yt_artist: str | None,
    yt_channel: str | None,
) -> tuple[str, str]:
    """Try to extract a clean Artist and Title.

    Priority:
    1. If the title contains "Artist - Title", split on the separator.
    2. Else use yt-dlp's ``artist`` field (if present and not a generic
       channel name).
    3. Fall back to the channel/uploader, stripping " - Topic".
    """
    cleaned = _clean_title(raw_title)

    # Try splitting "Artist - Title"
    parts = _ARTIST_TITLE_SEP.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()

    # Use yt-dlp artist if it looks real
    if yt_artist and yt_artist.lower() not in ("unknown", "unknown artist", ""):
        artist = yt_artist
        if artist.endswith(" - Topic"):
            artist = artist[: -len(" - Topic")]
        return artist.strip(), cleaned

    # Fall back to channel
    if yt_channel:
        channel = yt_channel
        if channel.endswith(" - Topic"):
            channel = channel[: -len(" - Topic")]
        return channel.strip(), cleaned

    return "Unknown Artist", cleaned


def _detect_source(info: dict) -> str:
    """Return a human-readable source label from yt-dlp info."""
    extractor = (info.get("extractor") or info.get("extractor_key") or "").lower()
    url = info.get("webpage_url") or info.get("url") or ""

    if "youtube" in extractor or "music.youtube" in url:
        if "music.youtube" in url:
            return "YouTube Music"
        return "YouTube"
    if "soundcloud" in extractor:
        return "SoundCloud"
    if "bandcamp" in extractor:
        return "Bandcamp"
    if "deezer" in extractor:
        return "Deezer"
    if "yandex" in extractor:
        return "Yandex Music"
    if "spotify" in extractor:
        return "Spotify"
    if extractor:
        return extractor.capitalize()
    return "Search"


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
    cleaned = _TITLE_PLATFORM_JUNK.sub("", raw_title).strip()
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
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": True,
        "cookiefile": "cookies.txt",
        "datebefore": CENSORSHIP_DATE,
    }


def _build_search_opts(output_dir: str, filename_prefix: str) -> dict:
    opts = _build_yt_dlp_opts(output_dir, filename_prefix)
    opts["default_search"] = "ytsearch10"
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
        if not info:
            return {}
        # When using ytsearch, yt-dlp returns a playlist wrapper.
        # Unwrap to the actual downloaded entry so metadata is correct.
        if info.get("_type") == "playlist" or (
            info.get("entries") and not info.get("artist") and not info.get("track")
        ):
            entries = [e for e in (info.get("entries") or []) if e]
            if entries:
                return entries[0]
        return info


def _extract_info_no_download(query: str) -> dict:
    """Fetch metadata only (no download) for date pre-checks."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "cookiefile": "cookies.txt",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        return info or {}


def _check_upload_date(info: dict) -> None:
    """Raise CensoredTrackError if upload_date >= CENSORSHIP_DATE."""
    upload_date = info.get("upload_date") or ""
    if upload_date and upload_date >= CENSORSHIP_DATE:
        raise CensoredTrackError(
            f"Track uploaded on {upload_date}, after censorship cutoff {CENSORSHIP_DATE}"
        )


def _find_downloaded_mp3(output_dir: str, prefix: str) -> list[str]:
    pattern = os.path.join(output_dir, f"{prefix}_*.mp3")
    return sorted(glob.glob(pattern))


def _parse_track_info(info: dict, filepath: str) -> TrackInfo:
    """Build a TrackInfo with cleaned artist/title and source detection."""
    raw_title = info.get("title") or info.get("track") or "Unknown Title"
    yt_artist = info.get("artist") or info.get("creator")
    yt_channel = info.get("uploader") or info.get("channel")

    artist, title = _smart_split_artist_title(raw_title, yt_artist, yt_channel)
    source = _detect_source(info)

    return TrackInfo(
        filepath=filepath,
        title=title,
        artist=artist,
        album=BRAND_ALBUM,
        duration=int(info.get("duration") or 0),
        source=source,
        thumbnail=info.get("thumbnail"),
    )


def _brand_metadata(filepath: str, artist: str, title: str) -> None:
    """Overwrite ID3 tags with cleaned metadata and IslandMusic branding."""
    try:
        audio = MP3(filepath)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("TPE1")
        audio.tags.add(TPE1(encoding=3, text=[artist]))
        audio.tags.delall("TIT2")
        audio.tags.add(TIT2(encoding=3, text=[title]))
        audio.tags.delall("TALB")
        audio.tags.add(TALB(encoding=3, text=[BRAND_ALBUM]))
        audio.tags.delall("COMM")
        audio.tags.add(
            COMM(encoding=3, lang="eng", desc="", text=[BRAND_COMMENT])
        )
        audio.save()
        logger.debug("Branded metadata: %s", filepath)
    except Exception:
        logger.warning("Failed to brand metadata for %s", filepath, exc_info=True)


def _rename_track_file(filepath: str, artist: str, title: str) -> str:
    """Rename downloaded file to 'Artist - Title.mp3'. Returns new path."""
    directory = os.path.dirname(filepath)
    ext = os.path.splitext(filepath)[1]
    safe_name = _UNSAFE_CHARS_RE.sub("_", f"{artist} - {title}")
    # Keep a short uuid prefix to avoid collisions
    prefix = os.path.basename(filepath)[:8]
    new_name = f"{prefix}_{safe_name}{ext}"
    new_path = os.path.join(directory, new_name)
    try:
        os.rename(filepath, new_path)
        return new_path
    except OSError:
        logger.warning("Could not rename %s -> %s", filepath, new_path)
        return filepath


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
        # Direct link: pre-check upload date before downloading
        logger.info("Direct URL — checking upload date: %s", query)
        info_pre = await asyncio.to_thread(_extract_info_no_download, query)
        _check_upload_date(info_pre)
        opts = _build_yt_dlp_opts(output_dir, prefix)
    else:
        opts = _build_search_opts(output_dir, prefix)
        search_query = f"{query} Official Audio"

    logger.info("Starting download: '%s'", search_query)
    info = await asyncio.to_thread(_extract_info, opts, search_query)

    files = _find_downloaded_mp3(output_dir, prefix)
    if not files:
        raise CensoredTrackError(
            f"No uncensored version found (uploaded before {CENSORSHIP_DATE}): {query}"
        )

    track = _parse_track_info(info, files[0])
    _brand_metadata(files[0], track.artist, track.title)
    new_path = _rename_track_file(files[0], track.artist, track.title)
    track.filepath = new_path

    logger.info("Finished download: '%s - %s' [%s]", track.artist, track.title, track.source)
    return track


# ── Batch download (with progress) ───────────────────────────────────

async def download_batch(
    queries: list[str],
    progress: BatchProgress,
    on_error: Callable[[str, Exception], Awaitable[None]] | None = None,
) -> list[TrackInfo]:
    """Download a list of queries one-by-one, updating *progress* after each."""
    downloaded: list[TrackInfo] = []

    for i, query in enumerate(queries, 1):
        progress.current_track = query
        progress.start_item()
        progress.notify()
        logger.info(
            "Starting download for track [%d] of [%d]: '%s'",
            i, progress.total, query,
        )
        try:
            track = await download_track(query)
            downloaded.append(track)
            progress.done += 1
            progress.finish_item()
            progress.current_track = f"{track.artist} - {track.title}"
            progress.source = track.source
            progress.notify()
            logger.info(
                "Finished downloading/converting track [%d]: '%s - %s' [%s]",
                i, track.artist, track.title, track.source,
            )
        except Exception as exc:
            progress.failed += 1
            progress.finish_item()
            progress.notify()
            logger.error(
                "Failed to download track [%d] of [%d] ('%s'): %s",
                i, progress.total, query, exc,
            )
            if on_error is not None:
                await on_error(query, exc)

    progress.current_track = ""
    progress.notify()
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
    entries = [entry for entry in (info.get("entries") or []) if entry]

    files = _find_downloaded_mp3(output_dir, prefix)
    if not files:
        raise FileNotFoundError(f"No MP3 files found after downloading album: {url}")

    tracks: list[TrackInfo] = []
    for i, filepath in enumerate(files):
        entry_info = entries[i] if i < len(entries) else info
        if progress:
            progress.start_item()
        track = _parse_track_info(entry_info, filepath)
        _brand_metadata(filepath, track.artist, track.title)
        new_path = _rename_track_file(filepath, track.artist, track.title)
        track.filepath = new_path
        tracks.append(track)
        if progress:
            progress.done += 1
            progress.finish_item()
            progress.current_track = f"{track.artist} - {track.title}"
            progress.source = track.source
            progress.notify()
            logger.info(
                "Finished downloading/converting track [%d] of [%d]: '%s - %s' [%s]",
                i + 1, progress.total, track.artist, track.title, track.source,
            )

    logger.info("Album download complete: %d tracks from %s", len(tracks), url)
    return tracks


async def get_album_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": False,
        "socket_timeout": 15,
        "cookiefile": "cookies.txt",
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


# ── ZIP archiving (with multi-part support) ──────────────────────────

TELEGRAM_FILE_LIMIT_BYTES: int = 50 * 1024 * 1024
ZIP_PART_MAX_BYTES: int = 49 * 1024 * 1024


def get_tracks_total_size(tracks: list[TrackInfo]) -> int:
    total = 0
    for t in tracks:
        if os.path.exists(t.filepath):
            total += os.path.getsize(t.filepath)
    return total


def _make_arc_name(index: int, track: TrackInfo) -> str:
    ext = os.path.splitext(track.filepath)[1]
    name = f"{index:02d} - {track.artist} - {track.title}{ext}"
    return _UNSAFE_CHARS_RE.sub("_", name)


def _partition_tracks_by_size(
    tracks: list[TrackInfo],
    max_bytes: int,
) -> list[list[TrackInfo]]:
    parts: list[list[TrackInfo]] = []
    current_part: list[TrackInfo] = []
    current_size = 0

    for track in tracks:
        if not os.path.exists(track.filepath):
            continue
        fsize = os.path.getsize(track.filepath)
        if current_part and current_size + fsize > max_bytes:
            parts.append(current_part)
            current_part = []
            current_size = 0
        current_part.append(track)
        current_size += fsize

    if current_part:
        parts.append(current_part)

    return parts


def _create_single_zip(
    tracks: list[TrackInfo],
    archive_path: str,
    track_offset: int = 0,
) -> str:
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, track in enumerate(tracks, track_offset + 1):
            if not os.path.exists(track.filepath):
                logger.warning("Skipping missing file: %s", track.filepath)
                continue
            zf.write(track.filepath, _make_arc_name(i, track))
        zf.writestr("README.txt", _README_CONTENT)
    size_mb = os.path.getsize(archive_path) / (1024 * 1024)
    logger.info("Created ZIP: %s (%.1fMB, %d tracks)", archive_path, size_mb, len(tracks))
    return archive_path


def _create_zip_archives(
    tracks: list[TrackInfo],
    base_name: str,
    progress: BatchProgress | None = None,
) -> list[str]:
    if progress:
        progress.status = BatchStatus.ARCHIVING
        progress.current_track = ""
        progress.notify()

    total_size = get_tracks_total_size(tracks)
    total_mb = total_size / (1024 * 1024)
    logger.info(
        "Starting ZIP compression: %s (%d tracks, %.1fMB raw)",
        base_name, len(tracks), total_mb,
    )

    needs_split = total_size > ZIP_PART_MAX_BYTES

    if needs_split:
        logger.info(
            "Total size %.1fMB exceeds %dMB limit — splitting into parts",
            total_mb, ZIP_PART_MAX_BYTES // (1024 * 1024),
        )
        if progress:
            progress.status = BatchStatus.SPLITTING
            progress.notify()

    parts = _partition_tracks_by_size(tracks, ZIP_PART_MAX_BYTES)

    archive_paths: list[str] = []
    stem = base_name.removesuffix(".zip")
    track_offset = 0

    for idx, part_tracks in enumerate(parts, 1):
        if len(parts) == 1:
            filename = base_name
        else:
            filename = f"{stem}_Part{idx}.zip"

        archive_path = os.path.join(config.downloads_dir, filename)

        if progress:
            progress.status = BatchStatus.ARCHIVING
            progress.current_track = f"Part {idx}/{len(parts)}"
            progress.notify()

        _create_single_zip(part_tracks, archive_path, track_offset)
        archive_paths.append(archive_path)
        track_offset += len(part_tracks)

    total_archive_mb = sum(
        os.path.getsize(p) / (1024 * 1024) for p in archive_paths
    )
    logger.info(
        "Finished ZIP compression. %d archive(s), total %.1fMB",
        len(archive_paths), total_archive_mb,
    )
    return archive_paths


async def create_zip_archives(
    tracks: list[TrackInfo],
    base_name: str,
    progress: BatchProgress | None = None,
) -> list[str]:
    return await asyncio.to_thread(
        _create_zip_archives, tracks, base_name, progress,
    )
