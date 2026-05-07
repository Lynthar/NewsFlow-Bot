"""Tests for SubscriptionRepository, focused on the seed-on-subscribe
behavior that prevents flooding a channel with a feed's back catalog,
plus the published_at age filter that stops feeds from re-serving their
archive.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository


def _settings_patch(max_age_days: int = 14):
    """Context manager: pin max_entry_publish_age_days for a test. Other
    settings attributes degrade to MagicMock — fine, repo only reads this
    one field."""
    fake = MagicMock()
    fake.max_entry_publish_age_days = max_age_days
    return patch(
        "newsflow.repositories.subscription_repository.get_settings",
        return_value=fake,
    )


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


async def test_seed_sent_entries_keep_latest_preserves_n_newest(session):
    """With keep_latest=1, the single newest entry stays unsent — used by
    subscribe() to deliver a preview to the user."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(timezone.utc)
    # 3 entries, newest last by hours_ago
    for i, hours_ago in enumerate([3, 2, 1]):
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=f"g{i}",
                title=f"Entry {i}",
                link=f"https://example.com/{i}",
                published_at=now - timedelta(hours=hours_ago),
            )
        )
    await session.flush()

    sub = Subscription(
        platform="test",
        platform_user_id="u1",
        platform_channel_id="c1",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    repo = SubscriptionRepository(session)
    seeded = await repo.seed_sent_entries(sub.id, feed.id, keep_latest=1)

    assert seeded == 2

    unsent = await repo.get_unsent_entries_for_subscription(sub.id)
    assert len(unsent) == 1
    assert unsent[0].guid == "g2"  # the newest (1 hour ago)


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


# ===== published_at age filter =====


async def test_unsent_filters_out_old_published_entries(session):
    """An entry whose published_at is older than the configured cap
    must NOT appear in unsent — this is the user-visible bug we're
    fixing (feeds re-serving year-old articles after cleanup)."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(timezone.utc)
    session.add_all([
        FeedEntry(
            feed_id=feed.id, guid="recent",
            title="recent", link="https://example.com/recent",
            published_at=now - timedelta(days=3),
        ),
        FeedEntry(
            feed_id=feed.id, guid="ancient",
            title="ancient", link="https://example.com/ancient",
            published_at=now - timedelta(days=400),
        ),
    ])
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    with _settings_patch(max_age_days=14):
        unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    guids = {e.guid for e in unsent}
    assert guids == {"recent"}


async def test_unsent_includes_entries_with_null_published_at(session):
    """published_at IS NULL must pass the age filter — some feeds don't
    carry a date and we'd rather deliver than silently drop them."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    session.add(
        FeedEntry(
            feed_id=feed.id, guid="no-date",
            title="no date", link="https://example.com/no-date",
            published_at=None,
        )
    )
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    with _settings_patch(max_age_days=14):
        unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    assert len(unsent) == 1
    assert unsent[0].guid == "no-date"


async def test_unsent_zero_disables_age_filter(session):
    """max_entry_publish_age_days=0 turns the filter off — even ancient
    entries flow through (back to pre-fix behavior, escape hatch)."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(timezone.utc)
    session.add(
        FeedEntry(
            feed_id=feed.id, guid="ancient",
            title="ancient", link="https://example.com/ancient",
            published_at=now - timedelta(days=400),
        )
    )
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    with _settings_patch(max_age_days=0):
        unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    assert len(unsent) == 1
    assert unsent[0].guid == "ancient"


async def test_set_silent_flips_single_subscription(session):
    feed = await _make_feed_with_entries(session, 0)
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    assert sub.silent is False  # default

    flipped = await repo.set_silent(
        platform=sub.platform,
        channel_id=sub.platform_channel_id,
        feed_id=feed.id,
        silent=True,
    )
    assert flipped is True

    await session.refresh(sub)
    assert sub.silent is True


async def test_set_silent_returns_false_when_no_match(session):
    repo = SubscriptionRepository(session)
    flipped = await repo.set_silent(
        platform="discord", channel_id="nope", feed_id=999, silent=True
    )
    assert flipped is False


async def test_set_channel_silent_flips_only_changed_rows(session):
    """Bulk-toggle skips rows already in the target state. A channel where
    one sub is already silent and another isn't should report flipped=1."""
    feed_a = Feed(url="https://example.com/a")
    feed_b = Feed(url="https://example.com/b")
    session.add_all([feed_a, feed_b])
    await session.flush()

    already_silent = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="chan",
        feed_id=feed_a.id,
        silent=True,
    )
    not_silent = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="chan",
        feed_id=feed_b.id,
        silent=False,
    )
    session.add_all([already_silent, not_silent])
    await session.flush()

    repo = SubscriptionRepository(session)
    flipped = await repo.set_channel_silent(
        platform="discord", channel_id="chan", silent=True
    )
    assert flipped == 1

    await session.refresh(already_silent)
    await session.refresh(not_silent)
    assert already_silent.silent is True
    assert not_silent.silent is True


async def test_unsent_age_filter_boundary(session):
    """Entry just inside the cutoff passes; just outside is filtered.
    Uses a 14-day cap with ±0.1 day from the boundary so we're nowhere
    near float-precision issues."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(timezone.utc)
    session.add_all([
        FeedEntry(
            feed_id=feed.id, guid="inside",
            title="inside", link="https://example.com/inside",
            published_at=now - timedelta(days=13.9),
        ),
        FeedEntry(
            feed_id=feed.id, guid="outside",
            title="outside", link="https://example.com/outside",
            published_at=now - timedelta(days=14.1),
        ),
    ])
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    with _settings_patch(max_age_days=14):
        unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    guids = {e.guid for e in unsent}
    assert guids == {"inside"}
