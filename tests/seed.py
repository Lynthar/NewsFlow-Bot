"""Builders several test files share: rows for tests that drive a handler against
the real database (the ``db`` fixture), and the doubles they build alike."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from newsflow.adapters.base import Message
from newsflow.adapters.discord.bot import DiscordAdapter
from newsflow.adapters.telegram.bot import TelegramAdapter
from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.services.dispatcher import Dispatcher

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


def unsaved_subscription(feed: Feed, **overrides: Any) -> Subscription:
    """A Telegram subscription on ``feed``, not yet added to any session."""
    defaults: dict[str, Any] = dict(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        target_language="en",
    )
    defaults.update(overrides)
    return Subscription(**defaults)


async def feed_with_entry(session: AsyncSession, **entry_overrides: Any) -> tuple[Feed, FeedEntry]:
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    fields: dict[str, Any] = dict(
        feed_id=feed.id,
        guid="e1",
        title="Big news",
        summary="A long and detailed summary of the big news that happened today",
        content=None,
        link="https://example.com/e1",
        image_url="https://example.com/pic.jpg",
    )
    fields.update(entry_overrides)
    row = FeedEntry(**fields)
    session.add(row)
    await session.commit()
    return feed, row


def message(**overrides: Any) -> Message:
    fields: dict[str, Any] = dict(title="T", summary="S", link="https://x.test/a", source="x.test")
    fields.update(overrides)
    return Message(**fields)


def dispatcher_with_adapter(platform: str, adapter: Any) -> Dispatcher:
    d = Dispatcher()
    d.register_adapter(platform, adapter)
    return d


def tg_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(token="test-token")
    adapter.app = MagicMock()
    adapter.app.bot.send_message = AsyncMock()
    return adapter


def discord_adapter() -> tuple[DiscordAdapter, MagicMock]:
    """The adapter, and the text channel its bot resolves every channel id to."""
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    adapter.bot = MagicMock()
    adapter.bot.get_channel = MagicMock(return_value=channel)
    return adapter, channel


def discord_interaction(channel_id: str) -> MagicMock:
    interaction = MagicMock()
    interaction.channel_id = int(channel_id)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def patch_feed_fetcher(
    monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]] | None = None
) -> None:
    """Stub the fetcher so add_feed succeeds without network I/O."""
    mock_fetcher = AsyncMock()
    mock_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="",
            success=True,
            entries=entries
            or [
                {
                    "guid": "e1",
                    "title": "First entry",
                    "link": "https://feed.example.com/e1",
                }
            ],
            etag=None,
            last_modified=None,
            feed_title="Test Feed",
            feed_description=None,
            feed_link=None,
        )
    )
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: mock_fetcher,
    )
