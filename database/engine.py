import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import config
from database.models import Base

_data_dir = os.path.dirname(config.database_url.replace("sqlite+aiosqlite:///", ""))
if _data_dir and not os.path.isabs(_data_dir):
    os.makedirs(_data_dir, exist_ok=True)

engine = create_async_engine(config.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
