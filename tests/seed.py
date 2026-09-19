"""Rows for tests that drive a handler against the real database (the ``db`` fixture)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription

SessionFactory = async_sessionmaker[AsyncSession]


async def subscription(
    factory: SessionFactory,
    *,
    platform: str = "telegram",
    channel_id: str = "555",
    user_id: str = "1",
    url: str = "https://ex.com/1",
    title: str | None = "Feed 1",
    feed_fields: dict[str, Any] | None = None,
    **fields: Any,
) -> Subscription:
    """Insert a subscription (and its feed, reused when the URL already exists).

    Extra keyword arguments are Subscription columns; ``feed_fields`` are Feed
    columns. Returns the committed row with ``feed`` loaded.
    """
    async with factory() as session:
        feed = await session.scalar(select(Feed).where(Feed.url == url))
        if feed is None:
            feed = Feed(
                url=url, title=title, **{"is_active": True, "error_count": 0, **(feed_fields or {})}
            )
            session.add(feed)
            await session.flush()
        sub = Subscription(
            platform=platform,
            platform_user_id=user_id,
            platform_channel_id=channel_id,
            feed_id=feed.id,
            **fields,
        )
        session.add(sub)
        await session.commit()
        return await _with_feed(session, sub.id)


async def entry(
    factory: SessionFactory,
    feed_id: int,
    *,
    guid: str,
    title: str = "Article",
    link: str = "https://ex.com/article",
    published_at: datetime | None = None,
    **fields: Any,
) -> FeedEntry:
    async with factory() as session:
        row = FeedEntry(
            feed_id=feed_id,
            guid=guid,
            title=title,
            link=link,
            published_at=published_at or datetime.now(UTC),
            **fields,
        )
        session.add(row)
        await session.commit()
        return row


async def subscription_row(factory: SessionFactory, sub_id: int) -> Subscription | None:
    """The subscription as the database has it now, with ``feed`` loaded."""
    async with factory() as session:
        return await _with_feed(session, sub_id)


async def _with_feed(session: AsyncSession, sub_id: int) -> Subscription | None:
    return await session.scalar(
        select(Subscription)
        .options(selectinload(Subscription.feed))
        .where(Subscription.id == sub_id)
    )
