"""
Feed management API endpoints.

Provides CRUD operations for feeds.
"""

from datetime import datetime
from typing import Sequence

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db
from newsflow.models.feed import Feed
from newsflow.repositories.feed_repository import FeedRepository
from newsflow.services.feed_service import FeedService

router = APIRouter()


# ===== Request/Response Models =====


class FeedCreate(BaseModel):
    """Request model for creating a feed."""

    url: HttpUrl


class FeedResponse(BaseModel):
    """Response model for a feed."""

    id: int
    url: str
    title: str | None
    description: str | None
    is_active: bool
    error_count: int
    last_error: str | None
    last_fetched_at: datetime | None
    last_successful_fetch_at: datetime | None
    created_at: datetime
    entry_count: int = 0

    class Config:
        from_attributes = True


class FeedListResponse(BaseModel):
    """Response model for feed list."""

    feeds: list[FeedResponse]
    total: int


class FeedTestResponse(BaseModel):
    """Response model for testing a feed."""

    success: bool
    title: str | None
    description: str | None
    entry_count: int
    error: str | None


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str


# ===== Helper Functions =====


async def _feed_to_response(
    feed: Feed,
    repo: FeedRepository,
) -> FeedResponse:
    """Convert a Feed model to response."""
    entry_count = await repo.count_entries(feed.id)
    return FeedResponse(
        id=feed.id,
        url=feed.url,
        title=feed.title,
        description=feed.description,
        is_active=feed.is_active,
        error_count=feed.error_count,
        last_error=feed.last_error,
        last_fetched_at=feed.last_fetched_at,
        last_successful_fetch_at=feed.last_successful_fetch_at,
        created_at=feed.created_at,
        entry_count=entry_count,
    )


# ===== Endpoints =====


@router.get("", response_model=FeedListResponse)
async def list_feeds(
    active_only: bool = False,
    db: AsyncSession = Depends(get_db),
) -> FeedListResponse:
    """
    List all feeds.

    Args:
        active_only: Only return active feeds
    """
    repo = FeedRepository(db)

    if active_only:
        feeds = await repo.get_all_active_feeds()
    else:
        from sqlalchemy import select

        result = await db.execute(select(Feed))
        feeds = result.scalars().all()

    feed_responses = []
    for feed in feeds:
        response = await _feed_to_response(feed, repo)
        feed_responses.append(response)

    return FeedListResponse(feeds=feed_responses, total=len(feed_responses))


@router.get("/{feed_id}", response_model=FeedResponse)
async def get_feed(
    feed_id: int,
    db: AsyncSession = Depends(get_db),
) -> FeedResponse:
    """Get a specific feed by ID."""
    repo = FeedRepository(db)
    feed = await repo.get_feed_by_id(feed_id)

    if not feed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feed {feed_id} not found",
        )

    return await _feed_to_response(feed, repo)


@router.post("", response_model=FeedResponse, status_code=status.HTTP_201_CREATED)
async def create_feed(
    feed_data: FeedCreate,
    db: AsyncSession = Depends(get_db),
) -> FeedResponse:
    """
    Add a new feed.

    The feed URL will be validated and fetched.
    """
    service = FeedService(db)
    repo = FeedRepository(db)

    result = await service.add_feed(str(feed_data.url))

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message,
        )

    return await _feed_to_response(result.feed, repo)


@router.delete("/{feed_id}", response_model=MessageResponse)
async def delete_feed(
    feed_id: int,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Delete a feed and all its entries."""
    repo = FeedRepository(db)

    feed = await repo.get_feed_by_id(feed_id)
    if not feed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feed {feed_id} not found",
        )

    await repo.delete_feed(feed_id)

    return MessageResponse(message=f"Feed {feed_id} deleted successfully")


@router.post("/{feed_id}/refresh", response_model=FeedResponse)
async def refresh_feed(
    feed_id: int,
    db: AsyncSession = Depends(get_db),
) -> FeedResponse:
    """Force refresh a feed."""
    repo = FeedRepository(db)
    service = FeedService(db)

    feed = await repo.get_feed_by_id(feed_id)
    if not feed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feed {feed_id} not found",
        )

    result = await service.fetch_and_store(feed)

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to refresh feed: {result.error}",
        )

    # Reload feed to get updated data
    await db.refresh(feed)
    return await _feed_to_response(feed, repo)


@router.post("/test", response_model=FeedTestResponse)
async def test_feed(
    feed_data: FeedCreate,
) -> FeedTestResponse:
    """
    Test a feed URL without adding it.

    Validates and fetches the feed to verify it works.
    """
    from newsflow.core import get_fetcher

    fetcher = get_fetcher()
    result = await fetcher.fetch_feed(str(feed_data.url))

    return FeedTestResponse(
        success=result.success,
        title=result.feed_title,
        description=result.feed_description,
        entry_count=len(result.entries) if result.success else 0,
        error=result.error,
    )
