"""
Statistics API endpoints.

Provides endpoints for viewing bot statistics.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import count, get_db
from newsflow.config import get_settings
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription

router = APIRouter()


class StatsResponse(BaseModel):
    """Overall statistics response."""

    total_feeds: int
    active_feeds: int
    total_entries: int
    total_subscriptions: int
    discord_subscriptions: int
    telegram_subscriptions: int
    translation_enabled: bool
    fetch_interval_minutes: int
    timestamp: str


class FeedStatsResponse(BaseModel):
    """Per-feed statistics."""

    feed_id: int
    url: str
    title: str | None
    entry_count: int
    subscription_count: int
    last_fetched_at: datetime | None
    is_active: bool


class FeedStatsListResponse(BaseModel):
    """Feed statistics list response."""

    feeds: list[FeedStatsResponse]


@router.get("", response_model=StatsResponse)
async def get_stats(
    db: AsyncSession = Depends(get_db),
) -> StatsResponse:
    """Get overall bot statistics."""
    settings = get_settings()

    feeds = select(func.count()).select_from(Feed)
    subs = select(func.count()).select_from(Subscription)

    return StatsResponse(
        total_feeds=await count(db, feeds),
        active_feeds=await count(db, feeds.where(Feed.is_active.is_(True))),
        total_entries=await count(db, select(func.count()).select_from(FeedEntry)),
        total_subscriptions=await count(db, subs),
        discord_subscriptions=await count(db, subs.where(Subscription.platform == "discord")),
        telegram_subscriptions=await count(db, subs.where(Subscription.platform == "telegram")),
        translation_enabled=settings.can_translate(),
        fetch_interval_minutes=settings.fetch_interval_minutes,
        timestamp=datetime.now(UTC).isoformat(),
    )


@router.get("/feeds", response_model=FeedStatsListResponse)
async def get_feed_stats(
    db: AsyncSession = Depends(get_db),
) -> FeedStatsListResponse:
    """Get per-feed statistics."""
    # Get all feeds with counts
    feeds_result = await db.execute(select(Feed))
    feeds = feeds_result.scalars().all()

    feed_stats = []
    for feed in feeds:
        entry_count = await count(
            db, select(func.count()).select_from(FeedEntry).where(FeedEntry.feed_id == feed.id)
        )
        sub_count = await count(
            db,
            select(func.count()).select_from(Subscription).where(Subscription.feed_id == feed.id),
        )

        feed_stats.append(
            FeedStatsResponse(
                feed_id=feed.id,
                url=feed.url,
                title=feed.title,
                entry_count=entry_count,
                subscription_count=sub_count,
                last_fetched_at=feed.last_fetched_at,
                is_active=feed.is_active,
            )
        )

    return FeedStatsListResponse(feeds=feed_stats)
