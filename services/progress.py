"""Shared progress state for batch operations.

The handler creates a ``BatchProgress`` instance and passes it into the
downloader.  The downloader mutates its fields and calls ``notify()``;
a background task in the handler layer waits on the event and edits the
Telegram message immediately (with a 2 s debounce).
"""

from __future__ import annotations

import asyncio
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
    source: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished: bool = False
    ema_track_seconds: float | None = None
    current_item_started_at: float | None = None
    # Set by the handler after creation — allows event-driven UI updates.
    _changed: asyncio.Event | None = field(default=None, repr=False)

    def attach_event(self, event: asyncio.Event) -> None:
        self._changed = event

    def notify(self) -> None:
        """Signal that state has changed so the UI updater wakes up."""
        if self._changed is not None:
            self._changed.set()

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def start_item(self) -> None:
        self.current_item_started_at = time.monotonic()

    def finish_item(self) -> None:
        if self.current_item_started_at is None:
            return
        duration = max(time.monotonic() - self.current_item_started_at, 0.0)
        alpha = 0.35
        if self.ema_track_seconds is None:
            self.ema_track_seconds = duration
        else:
            self.ema_track_seconds = (
                alpha * duration + (1 - alpha) * self.ema_track_seconds
            )
        self.current_item_started_at = None

    def eta_seconds(self) -> float:
        if self.ema_track_seconds is None:
            return 0.0
        remaining = self.total - self.done - self.failed
        return max(self.ema_track_seconds * remaining, 0.0)

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
        if self.source:
            lines.append(f"🌐 Источник: <b>{self.source}</b>")

        return "\n".join(lines)
