"""Tests for SubscriptionService pause/resume/detail."""

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.services.subscription_service import SubscriptionService


FEED_URL = "https://example.com/feed"


async def _seed_sub(session) -> Subscription:
    feed = Feed(url=FEED_URL, title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord",
        platform_user_id="u1",
        platform_channel_id="c1",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()
    return sub


async def test_pause_subscription_marks_inactive(session):
    sub = await _seed_sub(session)
    svc = SubscriptionService(session)

    result = await svc.pause_subscription(
        platform="discord", channel_id="c1", feed_url=FEED_URL,
    )

    assert result.success is True
    assert sub.is_active is False


async def test_pause_subscription_unknown_feed_fails(session):
    svc = SubscriptionService(session)

    result = await svc.pause_subscription(
        platform="discord", channel_id="c1", feed_url="https://nope.example/feed"
    )

    assert result.success is False
    assert "not found" in result.message.lower()


async def test_pause_subscription_unknown_sub_fails(session):
    # Feed exists but no subscription for this channel.
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()
    svc = SubscriptionService(session)

    result = await svc.pause_subscription(
        platform="discord", channel_id="other-channel", feed_url=feed.url
    )

    assert result.success is False


async def test_resume_subscription_reactivates(session):
    sub = await _seed_sub(session)
    sub.is_active = False
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_subscription(
        platform="discord", channel_id="c1", feed_url=FEED_URL
    )

    assert result.success is True
    assert sub.is_active is True


async def test_get_subscription_detail_returns_sub_and_recent_entries(session):
    sub = await _seed_sub(session)
    # Add 3 entries so detail has content.
    for i in range(3):
        session.add(
            FeedEntry(
                feed_id=sub.feed_id,
                guid=f"g{i}",
                title=f"Entry {i}",
                link=f"https://example.com/{i}",
            )
        )
    await session.flush()

    svc = SubscriptionService(session)
    detail = await svc.get_subscription_detail(
        platform="discord", channel_id="c1", feed_url=FEED_URL, entry_limit=2
    )

    assert detail is not None
    assert detail.subscription.id == sub.id
    assert detail.feed.id == sub.feed_id
    assert len(detail.recent_entries) == 2


async def test_get_subscription_detail_returns_none_for_missing_feed(session):
    svc = SubscriptionService(session)

    detail = await svc.get_subscription_detail(
        platform="discord", channel_id="c1", feed_url="https://nope.example/feed"
    )

    assert detail is None
