"""
FastAPI dependency injection.

Provides common dependencies for API routes.
"""

import hmac
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Header, HTTPException, status
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.config import get_settings
from newsflow.models.base import get_session_factory


def _token_matches(authorization: str | None, expected: str) -> bool:
    """Accepts ``Authorization: Bearer <key>`` or the raw key."""
    token = authorization or ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return bool(token) and hmac.compare_digest(token, expected)


async def require_api_key(
    authorization: str | None = Header(default=None),
) -> None:
    """Guard write endpoints with the shared API key.

    Fail-closed: if no ``api_key`` is configured, write access is refused
    entirely (503) rather than left open.
    """
    expected = get_settings().api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API write access is disabled (no api_key configured)",
        )
    if not _token_matches(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def require_ingest_key(
    authorization: str | None = Header(default=None),
) -> None:
    """Guard /api/ingest: INGEST_API_KEY or API_KEY. Fail-closed (503) when neither is
    configured. The ingest key opens nothing else."""
    settings = get_settings()
    keys = [k for k in (settings.ingest_api_key, settings.api_key) if k]
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingest is disabled (no ingest_api_key or api_key configured)",
        )
    if not any(_token_matches(authorization, k) for k in keys):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def require_read_api_key(
    authorization: str | None = Header(default=None),
) -> None:
    """Guard read endpoints — but only once a key exists.

    No configured key = reads stay open (writes are already fail-closed, and
    the default bind is loopback). As soon as API_KEY is set, every non-health
    endpoint requires it: the feed list exposes URLs that often embed tokens,
    and stats expose error details.
    """
    expected = get_settings().api_key
    if not expected:
        return
    if not _token_matches(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Get database session for request.

    Usage:
        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# SQLAlchemy 2.0 types select(func.count()) as Select[tuple[int]], 2.1 as Select[int].
async def count(db: AsyncSession, stmt: Select[Any]) -> int:
    return int(await db.scalar(stmt) or 0)
