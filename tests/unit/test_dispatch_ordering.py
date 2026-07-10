"""Unsent-entry ordering: oldest first.

Two user-facing guarantees ride on this ORDER BY:
- Chronology: within a batch, the newest article lands at the BOTTOM of
  the chat (sent last), reading like a timeline.
- Backlog fairness: a feed producing more than the per-cycle limit used
  to have its older entries permanently squeezed out by newer ones
  (newest-first + limit) until retention silently dropped them. Oldest
  first drains the backlog chronologically across cycles instead.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher


async def _seed(session, entries: list[tuple[str, datetime | None]]) -> Subscription:
    """Feed + subscription + entries given as (guid, published_at)."""
    feed = Feed(url="https://order.test/rss", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord", platform_user_id="u",
        platform_channel_id="c", feed_id=feed.id,
        is_active=True, translate=False,
    )
    session.add(sub)
    await session.flush()
    for guid, published_at in entries:
        session.add(
            FeedEntry(
                feed_id=feed.id, guid=guid, title=guid,
                link=f"https://order.test/{guid}",
                published_at=published_at,
            )
        )
    await session.commit()
    return sub


def _t(hours_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours_ago)


async def test_unsent_entries_come_back_oldest_first(session):
    # Inserted in scrambled order on purpose.
    sub = await _seed(session, [("mid", _t(2)), ("new", _t(1)), ("old", _t(3))])

    repo = SubscriptionRepository(session)
    entries = await repo.get_unsent_entries_for_subscription(sub.id, limit=10)

    assert [e.guid for e in entries] == ["old", "mid", "new"]


async def test_limit_keeps_the_oldest_not_the_newest(session):
    """With a backlog above the limit, the OLDEST batch goes out this
    cycle; newer entries wait their turn instead of evicting older ones."""
    sub = await _seed(
        session,
        [(f"e{i}", _t(10 - i)) for i in range(5)],  # e0 oldest … e4 newest
    )

    repo = SubscriptionRepository(session)
    entries = await repo.get_unsent_entries_for_subscription(sub.id, limit=3)

    assert [e.guid for e in entries] == ["e0", "e1", "e2"]


async def test_undated_entries_sort_first_and_ties_break_by_id(session):
    sub = await _seed(session, [("dated", _t(1)), ("undated", None)])

    repo = SubscriptionRepository(session)
    entries = await repo.get_unsent_entries_for_subscription(sub.id, limit=10)

    assert [e.guid for e in entries] == ["undated", "dated"]


async def test_dispatch_sends_oldest_first(session):
    """Full-path check: the adapter receives messages in chronological
    order, so the newest article ends up at the bottom of the channel."""
    sub = await _seed(session, [("new", _t(1)), ("old", _t(3)), ("mid", _t(2))])

    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        d = Dispatcher()

    sent_links: list[str] = []

    async def record(channel_id, message):
        sent_links.append(message.link)
        return True

    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=record)
    adapter.is_connected = MagicMock(return_value=True)
    d._adapters["discord"] = adapter

    repo = SubscriptionRepository(session)
    sent = await d._dispatch_to_subscription(session, sub, repo)
    await session.commit()

    assert sent == 3
    assert sent_links == [
        "https://order.test/old",
        "https://order.test/mid",
        "https://order.test/new",
    ]
