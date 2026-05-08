"""Integration tests for silent-mode subscriptions.

Silent subscriptions don't push instant messages to the channel, but
their entries are still marked as sent so the digest pipeline picks
them up via SentEntry. The post-subscribe preview path bypasses silent
so the user gets one confirmation article on /add.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher


def _dispatcher_with_adapter(platform: str, adapter) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = platform == "discord"
    fake.telegram_enabled = platform == "telegram"
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    fake.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        d = Dispatcher()
    d.register_adapter(platform, adapter)
    return d


async def _seed_recent_entry(session, feed: Feed, guid: str, title: str) -> FeedEntry:
    entry = FeedEntry(
        feed_id=feed.id,
        guid=guid,
        title=title,
        link=f"https://example.com/{guid}",
        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    session.add(entry)
    await session.flush()
    return entry


async def test_silent_subscription_marks_sent_without_delivery(session):
    """Silent sub: dispatcher must NOT call send_message but MUST persist
    a SentEntry row (was_filtered=False) so digest can find it."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    entry = await _seed_recent_entry(session, feed, "a", "Quiet news")

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        silent=True,
        translate=False,
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    d = _dispatcher_with_adapter("discord", adapter)
    sub_repo = SubscriptionRepository(session)

    sent_count = await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    # No instant delivery happened.
    assert sent_count == 0
    adapter.send_message.assert_not_awaited()

    # But SentEntry was persisted with was_filtered=False so digest sees it.
    rows = (
        await session.execute(
            select(SentEntry).where(SentEntry.subscription_id == sub.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].feed_id == entry.feed_id
    assert rows[0].guid == entry.guid
    assert rows[0].was_filtered is False


async def test_non_silent_subscription_delivers_normally(session):
    """Baseline: silent=False behaves exactly like before this feature."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    await _seed_recent_entry(session, feed, "a", "Loud news")

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        silent=False,
        translate=False,
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    d = _dispatcher_with_adapter("discord", adapter)
    sub_repo = SubscriptionRepository(session)

    sent_count = await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    assert sent_count == 1
    adapter.send_message.assert_awaited_once()


async def test_bypass_silent_delivers_one_article(session):
    """Preview path (dispatch_subscription) sets bypass_silent=True so the
    user sees one confirmation article even on a silent sub."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    await _seed_recent_entry(session, feed, "a", "Preview please")

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        silent=True,
        translate=False,
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    d = _dispatcher_with_adapter("discord", adapter)
    sub_repo = SubscriptionRepository(session)

    sent_count = await d._dispatch_to_subscription(
        session, sub, sub_repo, bypass_silent=True
    )
    await session.commit()

    assert sent_count == 1
    adapter.send_message.assert_awaited_once()


async def test_silent_respects_keyword_filter(session):
    """Filter takes precedence over silent: filtered entries get
    was_filtered=True (kept out of digest), not the silent path's
    was_filtered=False. Without this, a filtered entry would leak into
    the digest of a silent channel."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    pythony = await _seed_recent_entry(session, feed, "a", "Python release")
    jsy = await _seed_recent_entry(session, feed, "b", "JavaScript news")

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        silent=True,
        translate=False,
        filter_rule={"include_keywords": ["Python"], "exclude_keywords": []},
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    d = _dispatcher_with_adapter("discord", adapter)
    sub_repo = SubscriptionRepository(session)

    await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    # Nothing actually delivered (channel is silent).
    adapter.send_message.assert_not_awaited()

    # Both entries have SentEntry rows, but with different was_filtered.
    rows = (
        await session.execute(
            select(SentEntry).where(SentEntry.subscription_id == sub.id)
        )
    ).scalars().all()
    by_guid = {r.guid: r for r in rows}
    assert by_guid[pythony.guid].was_filtered is False  # silent path
    assert by_guid[jsy.guid].was_filtered is True       # filter path
