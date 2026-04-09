import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./data/islandmusic.db"
        )
    )
    downloads_dir: str = field(
        default_factory=lambda: os.getenv("DOWNLOADS_DIR", "./downloads")
    )
    max_files_per_batch: int = field(
        default_factory=lambda: int(os.getenv("MAX_FILES_PER_BATCH", "10"))
    )
    batch_delay_seconds: int = field(
        default_factory=lambda: int(os.getenv("BATCH_DELAY_SECONDS", "5"))
    )


config = Config()
