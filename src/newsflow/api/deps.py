"""
FastAPI dependency injection.

Provides common dependencies for API routes.
"""

import hmac
from typing import AsyncGenerator

from fastapi import Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.config import get_settings
from newsflow.models.base import get_session_factory


async def require_api_key(
    authorization: str | None = Header(default=None),
) -> None:
    """Guard write endpoints with the shared API key.

    Accepts ``Authorization: Bearer <key>`` or the raw key. Fail-closed: if no
    ``api_key`` is configured, write access is refused entirely (503) rather
    than left open.
    """
    expected = get_settings().api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API write access is disabled (no api_key configured)",
        )
    token = authorization or ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not hmac.compare_digest(token, expected):
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
