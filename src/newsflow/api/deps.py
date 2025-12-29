"""
FastAPI dependency injection.

Provides common dependencies for API routes.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.base import get_session_factory


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
