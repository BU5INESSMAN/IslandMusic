"""Shared progress state for batch operations.

The handler creates a ``BatchProgress`` instance and passes it into the
downloader.  The downloader mutates its fields; a background task in the
handler layer reads them periodically and edits the Telegram message.
Neither layer imports the other — they share only this thin data object.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class BatchStatus(enum.Enum):
    DOWNLOADING = "Скачивание"
    ARCHIVING = "Архивирование"
    SPLITTING = "Разбиваю на части"
    SENDING = "Отправка"
    DONE = "Готово"
    FAILED = "Ошибка"


@dataclass
class BatchProgress:
    total: int
    done: int = 0
    failed: int = 0
    status: BatchStatus = BatchStatus.DOWNLOADING
    current_track: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished: bool = False

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def eta_seconds(self) -> float:
        """Estimate remaining seconds based on average time per completed track."""
        if self.done == 0:
            return 0.0
        avg = self.elapsed_seconds() / self.done
        remaining = self.total - self.done - self.failed
        return max(avg * remaining, 0.0)

    def format_time(self, seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    def format_message(self) -> str:
        elapsed = self.format_time(self.elapsed_seconds())
        eta = self.format_time(self.eta_seconds())

        status_emoji = {
            BatchStatus.DOWNLOADING: "💿",
            BatchStatus.ARCHIVING: "🗜",
            BatchStatus.SPLITTING: "✂️",
            BatchStatus.SENDING: "📤",
            BatchStatus.DONE: "✅",
            BatchStatus.FAILED: "❌",
        }
        emoji = status_emoji.get(self.status, "💿")

        lines = [
            f"✅ Готово: <b>{self.done}</b> / <b>{self.total}</b>",
        ]
        if self.failed:
            lines.append(f"⚠️ Ошибки: <b>{self.failed}</b>")
        lines.extend([
            f"{emoji} Текущий статус: <b>{self.status.value}</b>",
            f"⏱ Прошло времени: <b>{elapsed}</b>",
            f"⌛️ Примерное время ожидания: <b>{eta}</b>",
        ])
        if self.current_track:
            lines.append(f"🎵 <i>{self.current_track}</i>")

        return "\n".join(lines)
