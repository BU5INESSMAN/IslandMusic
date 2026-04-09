from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import async_session
from database.models import DownloadHistory

logger = logging.getLogger(__name__)


async def log_download(
    user_id: int,
    query: str,
    source_url: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    status: str = "success",
) -> None:
    """Insert a single download record into the database."""
    try:
        async with async_session() as session:
            record = DownloadHistory(
                user_id=user_id,
                query=query,
                source_url=source_url,
                title=title,
                artist=artist,
                status=status,
            )
            session.add(record)
            await session.commit()
    except Exception:
        logger.exception("Failed to log download for user %d", user_id)


async def get_user_history(
    user_id: int, limit: int = 20,
) -> list[DownloadHistory]:
    """Return the most recent download records for a user."""
    async with async_session() as session:
        stmt = (
            select(DownloadHistory)
            .where(DownloadHistory.user_id == user_id)
            .order_by(DownloadHistory.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_user_download_count(user_id: int) -> int:
    """Return total number of downloads for a user."""
    from sqlalchemy import func

    async with async_session() as session:
        stmt = (
            select(func.count())
            .select_from(DownloadHistory)
            .where(DownloadHistory.user_id == user_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one()
