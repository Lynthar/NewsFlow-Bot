"""
Statistics API endpoints.

Provides endpoints for viewing bot statistics.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db
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

    # Count feeds
    total_feeds_result = await db.execute(select(func.count(Feed.id)))
    total_feeds = total_feeds_result.scalar_one()

    active_feeds_result = await db.execute(
        select(func.count(Feed.id)).where(Feed.is_active == True)
    )
    active_feeds = active_feeds_result.scalar_one()

    # Count entries
    total_entries_result = await db.execute(select(func.count(FeedEntry.id)))
    total_entries = total_entries_result.scalar_one()

    # Count subscriptions
    total_subs_result = await db.execute(select(func.count(Subscription.id)))
    total_subs = total_subs_result.scalar_one()

    discord_subs_result = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.platform == "discord")
    )
    discord_subs = discord_subs_result.scalar_one()

    telegram_subs_result = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.platform == "telegram")
    )
    telegram_subs = telegram_subs_result.scalar_one()

    return StatsResponse(
        total_feeds=total_feeds,
        active_feeds=active_feeds,
        total_entries=total_entries,
        total_subscriptions=total_subs,
        discord_subscriptions=discord_subs,
        telegram_subscriptions=telegram_subs,
        translation_enabled=settings.can_translate(),
        fetch_interval_minutes=settings.fetch_interval_minutes,
        timestamp=datetime.now(timezone.utc).isoformat(),
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
        # Count entries
        entry_count_result = await db.execute(
            select(func.count(FeedEntry.id)).where(FeedEntry.feed_id == feed.id)
        )
        entry_count = entry_count_result.scalar_one()

        # Count subscriptions
        sub_count_result = await db.execute(
            select(func.count(Subscription.id)).where(Subscription.feed_id == feed.id)
        )
        sub_count = sub_count_result.scalar_one()

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
