"""Shared pytest fixtures."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from newsflow.models.base import Base


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """In-memory SQLite session with schema created. One engine per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as s:
        yield s

    await engine.dispose()
