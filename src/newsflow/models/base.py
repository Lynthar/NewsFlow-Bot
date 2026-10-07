"""
SQLAlchemy base configuration and database utilities.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from newsflow.config import get_settings


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Configure SQLite for safe, concurrent bot use. Applied per-connection
    because SQLite's pragmas are connection-scoped (not database-scoped).

    - `foreign_keys=ON`: enforce FK constraints. SQLite ships with this
      OFF by default, so `ON DELETE CASCADE` is silently ignored without
      it — deleting a Subscription would orphan SentEntry rows and
      subsequent /feed add races hit UNIQUE constraint errors when
      seed_sent_entries tries to re-insert the same pair.

    - `journal_mode=WAL`: Write-Ahead Logging. Readers and writers no
      longer block each other — a reader sees a consistent snapshot
      while a writer appends to the WAL file. Without this, a long
      dispatch cycle holding its session open causes concurrent
      commands (e.g. `/digest enable`) to raise
      "database is locked". WAL persists per-DB-file, so once set
      it's sticky across restarts.

    - `synchronous=NORMAL`: under WAL, this is the safe sweet spot —
      faster than FULL with the same durability guarantees across
      application crashes (only an OS-level crash or power loss can
      lose the last committed transaction, which for a bot is fine).

    Narrows to SQLite so non-SQLite backends (asyncpg etc.) are
    unaffected.
    """
    if "sqlite" not in dbapi_connection.__class__.__module__.lower():
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


# Naming convention for constraints (important for migrations)
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all models."""

    metadata = MetaData(naming_convention=convention)

    # Common columns. created_at/updated_at are DateTime(timezone=True) with a
    # tz-AWARE UTC default — binding those to a naive TIMESTAMP raises DataError on
    # asyncpg, so the Postgres column must be timestamptz.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


# Engine and session factory (lazy initialization)
_engine = None
_async_session_factory = None


def get_engine() -> AsyncEngine:
    """Get or create the database engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args: dict[str, Any] = {}
        is_sqlite = settings.database_url.startswith("sqlite")
        if is_sqlite:
            # aiosqlite timeout maps to sqlite3's busy handler: wait up to 15s for another
            # writer instead of raising "database is locked". With WAL this removes the
            # dispatch-loop vs slash-command contention.
            connect_args["timeout"] = 15
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            # Bound values ride in every DB exception's text, webhook secrets and auth
            # headers included; DB_ECHO is the explicit opt-in to seeing them.
            hide_parameters=not settings.db_echo,
            # A server restart leaves every pooled connection dead; test each on checkout
            # instead of failing the first query on it. SQLite has no server to lose.
            pool_pre_ping=not is_sqlite,
            future=True,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the session factory."""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting database sessions.

    Usage:
        async with get_session() as session:
            # use session
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database tables."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connections."""
    global _engine, _async_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
