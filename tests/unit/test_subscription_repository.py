"""Tests for SubscriptionRepository, focused on the seed-on-subscribe
behavior that prevents flooding a channel with a feed's back catalog.
"""

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository


async def _make_feed_with_entries(session, n: int) -> Feed:
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()
    for i in range(n):
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=f"guid-{i}",
                title=f"title {i}",
                link=f"https://example.com/{i}",
            )
        )
    await session.flush()
    return feed


async def _make_subscription(session, feed_id: int) -> Subscription:
    sub = Subscription(
        platform="test",
        platform_user_id="user-1",
        platform_channel_id="chan-1",
        feed_id=feed_id,
    )
    session.add(sub)
    await session.flush()
    return sub


async def test_seed_sent_entries_marks_all_existing(session):
    feed = await _make_feed_with_entries(session, 3)
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    seeded = await repo.seed_sent_entries(sub.id, feed.id)

    assert seeded == 3
    unsent = await repo.get_unsent_entries_for_subscription(sub.id)
    assert list(unsent) == []


async def test_seed_sent_entries_empty_feed(session):
    feed = await _make_feed_with_entries(session, 0)
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    seeded = await repo.seed_sent_entries(sub.id, feed.id)

    assert seeded == 0


async def test_entries_added_after_seed_are_unsent(session):
    feed = await _make_feed_with_entries(session, 2)
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)
    await repo.seed_sent_entries(sub.id, feed.id)

    # New entry arrives after seeding — must show up as unsent.
    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="guid-new",
            title="new",
            link="https://example.com/new",
        )
    )
    await session.flush()

    unsent = await repo.get_unsent_entries_for_subscription(sub.id)
    assert len(unsent) == 1
    assert unsent[0].guid == "guid-new"
