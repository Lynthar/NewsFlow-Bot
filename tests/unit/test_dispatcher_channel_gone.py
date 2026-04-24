"""Tests for the ChannelGoneError auto-deactivate flow.

When an adapter signals that a channel is permanently unreachable
(Discord 404 / Telegram "chat not found" / bot kicked), the
dispatcher must:
  1. Deactivate every subscription for (platform, channel_id).
  2. Disable any ChannelDigest for that channel.
  3. Not raise — dispatch continues to the next subscription.

The common production scenario this protects against: a user deletes
a Discord channel and creates a new one. The old channel id is gone
forever (snowflake ids are never reused), so keeping subs active
just burns one failed API call per dispatch cycle per sub.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from newsflow.adapters.base import ChannelGoneError
from newsflow.models.digest import ChannelDigest
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher

# ===== Repository-layer tests =====


async def test_deactivate_channel_flips_only_matching_active_rows(session):
    # Separate feeds because (platform, channel_id, feed_id) is UNIQUE —
    # a single channel can only be subscribed once per feed.
    feed_a = Feed(url="https://a.test/rss", is_active=True, error_count=0)
    feed_b = Feed(url="https://b.test/rss", is_active=True, error_count=0)
    feed_c = Feed(url="https://c.test/rss", is_active=True, error_count=0)
    session.add_all([feed_a, feed_b, feed_c])
    await session.flush()

    # Two active subs on the dead channel (different feeds) + one
    # already-inactive sub on DEAD (proves WHERE is_active=True skips
    # it) + ALIVE sub + a discord_DEAD lookalike on Telegram.
    session.add_all([
        Subscription(
            platform="discord", platform_user_id="u",
            platform_channel_id="DEAD", feed_id=feed_a.id, is_active=True,
        ),
        Subscription(
            platform="discord", platform_user_id="u",
            platform_channel_id="DEAD", feed_id=feed_b.id, is_active=True,
        ),
        Subscription(
            platform="discord", platform_user_id="u",
            platform_channel_id="DEAD", feed_id=feed_c.id, is_active=False,
        ),
        Subscription(
            platform="discord", platform_user_id="u",
            platform_channel_id="ALIVE", feed_id=feed_a.id, is_active=True,
        ),
        Subscription(
            platform="telegram", platform_user_id="u",
            platform_channel_id="DEAD", feed_id=feed_a.id, is_active=True,
        ),
    ])
    await session.commit()

    repo = SubscriptionRepository(session)
    flipped = await repo.deactivate_channel("discord", "DEAD")
    await session.commit()

    # Two active discord-DEAD rows flipped. The already-inactive one
    # doesn't count (WHERE is_active=True skips it).
    assert flipped == 2

    # ALIVE and telegram-DEAD untouched.
    alive = await session.execute(
        select(Subscription).where(Subscription.platform_channel_id == "ALIVE")
    )
    assert alive.scalar_one().is_active is True

    tg = await session.execute(
        select(Subscription).where(
            Subscription.platform == "telegram",
            Subscription.platform_channel_id == "DEAD",
        )
    )
    assert tg.scalar_one().is_active is True


async def test_deactivate_channel_second_call_is_noop(session):
    feed = Feed(url="https://b.test/rss", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    session.add(
        Subscription(
            platform="discord", platform_user_id="u",
            platform_channel_id="DEAD", feed_id=feed.id, is_active=True,
        )
    )
    await session.commit()

    repo = SubscriptionRepository(session)
    assert await repo.deactivate_channel("discord", "DEAD") == 1
    await session.commit()
    # Second call: no active rows left to flip.
    assert await repo.deactivate_channel("discord", "DEAD") == 0


async def test_disable_for_channel_flips_matching_digest(session):
    session.add_all([
        ChannelDigest(
            platform="discord", platform_channel_id="DEAD",
            enabled=True, schedule="daily", delivery_hour_utc=9,
            language="en", include_filtered=False, max_articles=50,
        ),
        ChannelDigest(
            platform="discord", platform_channel_id="ALIVE",
            enabled=True, schedule="daily", delivery_hour_utc=9,
            language="en", include_filtered=False, max_articles=50,
        ),
    ])
    await session.commit()

    repo = ChannelDigestRepository(session)
    flipped = await repo.disable_for_channel("discord", "DEAD")
    await session.commit()

    assert flipped == 1

    # ALIVE is untouched.
    alive = await session.execute(
        select(ChannelDigest).where(
            ChannelDigest.platform_channel_id == "ALIVE"
        )
    )
    assert alive.scalar_one().enabled is True


async def test_disable_for_channel_missing_row_is_safe_noop(session):
    repo = ChannelDigestRepository(session)
    assert await repo.disable_for_channel("discord", "does-not-exist") == 0


# ===== Dispatcher-layer tests =====


def _dispatcher() -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


async def _seed_sub_with_entry(session, *, channel_id: str) -> Subscription:
    feed = Feed(
        url=f"https://{channel_id}.test/rss",
        is_active=True, error_count=0,
    )
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord", platform_user_id="u",
        platform_channel_id=channel_id, feed_id=feed.id,
        is_active=True, translate=False,
    )
    session.add(sub)
    await session.flush()
    # One unsent entry the dispatcher can try to deliver.
    entry = FeedEntry(
        feed_id=feed.id, guid="g1", title="Title",
        link=f"https://{channel_id}.test/a",
        published_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    session.add(entry)
    await session.commit()
    return sub


async def test_dispatch_catches_channel_gone_and_deactivates(session):
    d = _dispatcher()

    sub = await _seed_sub_with_entry(session, channel_id="DEAD")

    adapter = MagicMock()
    adapter.send_message = AsyncMock(
        side_effect=ChannelGoneError("DEAD", reason="404 Not Found")
    )
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    sub_repo = SubscriptionRepository(session)
    d._adapters["discord"] = adapter

    # Must not raise — dispatcher swallows ChannelGoneError after
    # doing the cleanup.
    sent = await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    assert sent == 0

    refreshed = await session.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )
    assert refreshed.scalar_one().is_active is False


async def test_dispatch_channel_gone_also_disables_digest(session):
    d = _dispatcher()

    sub = await _seed_sub_with_entry(session, channel_id="DEAD")
    session.add(
        ChannelDigest(
            platform="discord", platform_channel_id="DEAD",
            enabled=True, schedule="daily", delivery_hour_utc=9,
            language="en", include_filtered=False, max_articles=50,
        )
    )
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(
        side_effect=ChannelGoneError("DEAD")
    )
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    d._adapters["discord"] = adapter

    sub_repo = SubscriptionRepository(session)
    await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    digest = await session.execute(
        select(ChannelDigest).where(
            ChannelDigest.platform_channel_id == "DEAD"
        )
    )
    assert digest.scalar_one().enabled is False


async def test_dispatch_channel_gone_second_call_is_idempotent(session):
    """Simulate the dispatch cycle hitting TWO subs on the same dead
    channel: the second call should deactivate zero rows (all already
    flipped) and not explode."""
    d = _dispatcher()

    feed_a = Feed(url="https://a/rss", is_active=True, error_count=0)
    feed_b = Feed(url="https://b/rss", is_active=True, error_count=0)
    session.add_all([feed_a, feed_b])
    await session.flush()

    sub_a = Subscription(
        platform="discord", platform_user_id="u",
        platform_channel_id="DEAD", feed_id=feed_a.id,
        is_active=True, translate=False,
    )
    sub_b = Subscription(
        platform="discord", platform_user_id="u",
        platform_channel_id="DEAD", feed_id=feed_b.id,
        is_active=True, translate=False,
    )
    session.add_all([sub_a, sub_b])
    await session.flush()
    for feed, sub in [(feed_a, sub_a), (feed_b, sub_b)]:
        session.add(
            FeedEntry(
                feed_id=feed.id, guid=f"g{feed.id}", title="T",
                link=f"https://{feed.id}/a",
                published_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=ChannelGoneError("DEAD"))
    adapter.is_connected = MagicMock(return_value=True)
    d._adapters["discord"] = adapter

    sub_repo = SubscriptionRepository(session)
    await d._dispatch_to_subscription(session, sub_a, sub_repo)
    # Second call in the same "cycle": channel already deactivated.
    # Adapter still raises (the subscription object is stale), but
    # the repo UPDATE no-ops. Must not crash.
    await d._dispatch_to_subscription(session, sub_b, sub_repo)
    await session.commit()

    # Both subs are inactive.
    all_subs = await session.execute(
        select(Subscription).where(Subscription.platform_channel_id == "DEAD")
    )
    assert all(s.is_active is False for s in all_subs.scalars().all())


async def test_dispatch_channel_gone_other_channel_unaffected(session):
    """A dead channel's cleanup must not disable a healthy channel's
    subs or digest."""
    d = _dispatcher()

    dead_sub = await _seed_sub_with_entry(session, channel_id="DEAD")
    alive_sub = await _seed_sub_with_entry(session, channel_id="ALIVE")
    session.add_all([
        ChannelDigest(
            platform="discord", platform_channel_id="DEAD",
            enabled=True, schedule="daily", delivery_hour_utc=9,
            language="en", include_filtered=False, max_articles=50,
        ),
        ChannelDigest(
            platform="discord", platform_channel_id="ALIVE",
            enabled=True, schedule="daily", delivery_hour_utc=9,
            language="en", include_filtered=False, max_articles=50,
        ),
    ])
    await session.commit()

    adapter = MagicMock()

    async def fake_send(channel_id, _msg):
        if channel_id == "DEAD":
            raise ChannelGoneError(channel_id)
        return True

    adapter.send_message = AsyncMock(side_effect=fake_send)
    adapter.is_connected = MagicMock(return_value=True)
    d._adapters["discord"] = adapter

    sub_repo = SubscriptionRepository(session)
    await d._dispatch_to_subscription(session, dead_sub, sub_repo)
    await d._dispatch_to_subscription(session, alive_sub, sub_repo)
    await session.commit()

    dead_refreshed = await session.execute(
        select(Subscription).where(Subscription.id == dead_sub.id)
    )
    alive_refreshed = await session.execute(
        select(Subscription).where(Subscription.id == alive_sub.id)
    )
    assert dead_refreshed.scalar_one().is_active is False
    assert alive_refreshed.scalar_one().is_active is True

    dead_digest = await session.execute(
        select(ChannelDigest).where(
            ChannelDigest.platform_channel_id == "DEAD"
        )
    )
    alive_digest = await session.execute(
        select(ChannelDigest).where(
            ChannelDigest.platform_channel_id == "ALIVE"
        )
    )
    assert dead_digest.scalar_one().enabled is False
    assert alive_digest.scalar_one().enabled is True


async def test_channel_gone_error_carries_channel_id():
    """The exception object preserves the channel id (used for logging
    and the deactivate lookup). Also preserves the reason string."""
    e = ChannelGoneError("CHAN-123", reason="404 Not Found")
    assert e.channel_id == "CHAN-123"
    assert e.reason == "404 Not Found"
    assert "CHAN-123" in str(e)
    assert "404" in str(e)

    e2 = ChannelGoneError("CHAN-456")
    assert e2.channel_id == "CHAN-456"
    assert e2.reason == ""
