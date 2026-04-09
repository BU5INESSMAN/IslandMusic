# 🌴 IslandMusic - Project Guidelines
## Project Overview
IslandMusic is a production-ready Telegram bot designed to download high-quality audio files (singles and entire albums) from various sources via `yt-dlp`. It is optimized for car stereo compatibility (strict ID3 tags & cover art) and features anti-flood mechanisms for bulk downloading.
## Tech Stack
- **Python:** 3.11+
- **Telegram Framework:** `aiogram` 3.x
- **Downloader:** `yt-dlp`
- **Post-Processing:** `ffmpeg` (Required for cover art and conversion)
- **Database:** SQLAlchemy 2.0 (Async) + SQLite
- **Deployment:** Docker & Docker Compose
## Core Architecture & Rules
- **Async First:** Run heavy sync tasks (`yt-dlp`) via `asyncio.to_thread()`.
- **Metadata is Mandatory:** Format to MP3/M4A. Embed Title, Artist, Album, and Cover Art.
- **Cleanup:** Delete local files from `downloads/` immediately after uploading to Telegram.
- **Anti-Spam Queue:** Max 10 files per 5-10 seconds. Catch `TelegramRetryAfter`.
- **Style:** Type hints required, strict separation of concerns, HTML parse mode.
