"""Tests for SubscriptionRepository, focused on the seed-on-subscribe
behavior that prevents flooding a channel with a feed's back catalog,
plus the published_at age filter that stops feeds from re-serving their
archive.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService


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

    # Seeded rows must carry seeded=True so the digest pipeline excludes
    # backlog the channel never actually received.
    from sqlalchemy import select

    from newsflow.models.subscription import SentEntry

    rows = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == sub.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert all(r.seeded is True for r in rows)
    assert all(r.was_filtered is False for r in rows)


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

    now = datetime.now(UTC)
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


async def test_seed_keep_latest_picks_the_newest_by_published_at_not_by_id(session):
    """The preview is the most recently published entry; insertion order is
    no proxy — a backfilling feed inserts its oldest articles last."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(UTC)
    for guid, hours_ago in [("old", 3), ("newest", 1), ("middle", 2)]:
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=guid,
                title=guid,
                link=f"https://example.com/{guid}",
                published_at=now - timedelta(hours=hours_ago),
            )
        )
        await session.flush()  # ids ascend in insertion order: old < newest < middle
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    assert await repo.seed_sent_entries(sub.id, feed.id, keep_latest=1) == 2

    unsent = await repo.get_unsent_entries_for_subscription(sub.id)
    assert [e.guid for e in unsent] == ["newest"]


async def test_unsent_is_tracked_per_subscription_not_per_feed(session):
    """SentEntry is keyed by (subscription, feed, guid): one channel receiving
    an entry says nothing about another channel on the same feed."""
    feed = await _make_feed_with_entries(session, 2)
    first = await _make_subscription(session, feed.id)
    second = Subscription(
        platform="test", platform_user_id="user-2", platform_channel_id="chan-2", feed_id=feed.id
    )
    session.add(second)
    await session.flush()
    repo = SubscriptionRepository(session)

    await repo.mark_entry_sent(first.id, feed.id, "guid-0")

    assert {e.guid for e in await repo.get_unsent_entries_for_subscription(first.id)} == {"guid-1"}
    assert {e.guid for e in await repo.get_unsent_entries_for_subscription(second.id)} == {
        "guid-0",
        "guid-1",
    }


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


async def test_unsent_filters_out_old_published_entries(session, configure):
    """An entry whose published_at is older than the configured cap
    must NOT appear in unsent — this is the user-visible bug we're
    fixing (feeds re-serving year-old articles after cleanup)."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(UTC)
    session.add_all(
        [
            FeedEntry(
                feed_id=feed.id,
                guid="recent",
                title="recent",
                link="https://example.com/recent",
                published_at=now - timedelta(days=3),
            ),
            FeedEntry(
                feed_id=feed.id,
                guid="ancient",
                title="ancient",
                link="https://example.com/ancient",
                published_at=now - timedelta(days=400),
            ),
        ]
    )
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    configure(max_entry_publish_age_days=14)
    unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    guids = {e.guid for e in unsent}
    assert guids == {"recent"}


async def test_unsent_includes_entries_with_null_published_at(session, configure):
    """published_at IS NULL must pass the age filter — some feeds don't
    carry a date and we'd rather deliver than silently drop them."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="no-date",
            title="no date",
            link="https://example.com/no-date",
            published_at=None,
        )
    )
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    configure(max_entry_publish_age_days=14)
    unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    assert len(unsent) == 1
    assert unsent[0].guid == "no-date"


async def test_unsent_zero_disables_age_filter(session, configure):
    """max_entry_publish_age_days=0 turns the filter off — even ancient
    entries flow through (back to pre-fix behavior, escape hatch)."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(UTC)
    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="ancient",
            title="ancient",
            link="https://example.com/ancient",
            published_at=now - timedelta(days=400),
        )
    )
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    configure(max_entry_publish_age_days=0)
    unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    assert len(unsent) == 1
    assert unsent[0].guid == "ancient"


async def _feed_with_recent_and_ancient(session) -> tuple[Feed, Subscription]:
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()
    now = datetime.now(UTC)
    for guid, age in [("recent", timedelta(days=3)), ("ancient", timedelta(days=400))]:
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=guid,
                title=guid,
                link=f"https://example.com/{guid}",
                published_at=now - age,
            )
        )
    sub = await _make_subscription(session, feed.id)
    return feed, sub


async def test_age_window_of_one_day_is_enforced(session, configure):
    """1 is the smallest window that is still a window — only 0 disables it."""
    _, sub = await _feed_with_recent_and_ancient(session)
    repo = SubscriptionRepository(session)

    configure(max_entry_publish_age_days=1)

    assert list(await repo.get_unsent_entries_for_subscription(sub.id)) == []
    assert await repo.count_unsent_entries_for_subscription(sub.id) == 0


async def test_count_unsent_applies_the_same_age_window_as_delivery(session, configure):
    """/feed status counts the backlog dispatch will actually deliver: the age
    window applies, and max_entry_publish_age_days=0 turns it off."""
    _, sub = await _feed_with_recent_and_ancient(session)
    repo = SubscriptionRepository(session)

    configure(max_entry_publish_age_days=14)
    assert await repo.count_unsent_entries_for_subscription(sub.id) == 1
    configure(max_entry_publish_age_days=0)
    assert await repo.count_unsent_entries_for_subscription(sub.id) == 2


async def test_count_unsent_is_zero_for_an_unknown_subscription(session):
    repo = SubscriptionRepository(session)
    assert await repo.count_unsent_entries_for_subscription(999_999) == 0


async def test_update_channel_subscriptions_narrowed_to_one_feed(session):
    feed = await _make_feed_with_entries(session, 0)
    other = Feed(url="https://example.com/other")
    session.add(other)
    await session.flush()
    sub = await _make_subscription(session, feed.id)
    neighbour = await _make_subscription(session, other.id)
    repo = SubscriptionRepository(session)

    matched = await repo.update_channel_subscriptions(
        sub.platform, sub.platform_channel_id, feed_id=feed.id, silent=True
    )
    assert matched == 1

    await session.refresh(sub)
    await session.refresh(neighbour)
    assert (sub.silent, neighbour.silent) == (True, False)


async def test_update_channel_subscriptions_reports_no_match(session):
    repo = SubscriptionRepository(session)
    assert await repo.update_channel_subscriptions("discord", "nope", feed_id=999, silent=True) == 0


async def test_channel_update_leaves_the_same_channel_id_on_another_platform_alone(session):
    feed = Feed(url="https://example.com/a")
    session.add(feed)
    await session.flush()
    subs = [
        Subscription(platform=p, platform_user_id="u", platform_channel_id="555", feed_id=feed.id)
        for p in ("discord", "telegram")
    ]
    session.add_all(subs)
    await session.flush()

    await SubscriptionRepository(session).update_channel_subscriptions(
        "discord", "555", message_template="{title}"
    )

    for sub in subs:
        await session.refresh(sub)
    assert [s.message_template for s in subs] == ["{title}", None]


async def test_get_or_create_subscription_applies_silent_to_new(session):
    feed = Feed(url="https://example.com/a")
    session.add(feed)
    await session.flush()

    repo = SubscriptionRepository(session)
    sub, created = await repo.get_or_create_subscription(
        platform="discord",
        user_id="u",
        channel_id="c",
        feed_id=feed.id,
        silent=True,
    )

    assert created is True
    assert sub.silent is True


async def test_get_or_create_subscription_preserves_silent_on_existing(session):
    """Re-subscribing must NOT silently flip an existing sub's silent
    state — only fresh creates apply the caller's silent argument."""
    feed = Feed(url="https://example.com/a")
    session.add(feed)
    await session.flush()
    existing = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        silent=True,
    )
    session.add(existing)
    await session.flush()

    repo = SubscriptionRepository(session)
    sub, created = await repo.get_or_create_subscription(
        platform="discord",
        user_id="u",
        channel_id="c",
        feed_id=feed.id,
        silent=False,  # caller passes False; existing was True
    )

    assert created is False
    assert sub.silent is True  # preserved


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
    flipped = await repo.set_channel_silent(platform="discord", channel_id="chan", silent=True)
    assert flipped == 1

    await session.refresh(already_silent)
    await session.refresh(not_silent)
    assert already_silent.silent is True
    assert not_silent.silent is True


async def test_cleanup_rediscover_no_longer_redelivers(session):
    """End-to-end regression for the 2026-05-08 SentEntry schema change.

    Before: cleanup deleted FeedEntry -> CASCADE deleted SentEntry ->
    next fetch re-created FeedEntry (same guid, new id) -> dispatcher
    saw no SentEntry match and re-delivered to channels that already
    saw the article.

    After: SentEntry is keyed on (feed_id, guid), no FK to FeedEntry.
    Same scenario: SentEntry survives FeedEntry cleanup, re-ingestion
    is recognized as already-seen, dispatch returns no unsent entries.
    """
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    entry = FeedEntry(
        feed_id=feed.id,
        guid="reborn",
        title="Article",
        link="https://example.com/a",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(entry)
    await session.flush()

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    repo = SubscriptionRepository(session)

    # Step 1: dispatch sends + marks the entry sent.
    await repo.mark_entry_sent(
        subscription_id=sub.id,
        feed_id=feed.id,
        guid="reborn",
        was_filtered=False,
    )

    # Step 2: cleanup deletes the FeedEntry (aged out by created_at).
    await session.delete(entry)
    await session.flush()

    # Step 3: the source re-serves the same guid and fetch creates a fresh FeedEntry.
    # SQLite may reuse rowids — fine, dedupe no longer depends on FeedEntry.id.
    reborn = FeedEntry(
        feed_id=feed.id,
        guid="reborn",
        title="Article",
        link="https://example.com/a",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(reborn)
    await session.flush()

    # Step 4: dispatcher asks for unsent entries — must come back empty
    # because SentEntry still has the (feed_id, guid) signal regardless
    # of which FeedEntry.id the new row got.
    unsent = await repo.get_unsent_entries_for_subscription(sub.id)
    assert list(unsent) == []


async def test_unsent_age_filter_boundary(session, configure):
    """Entry just inside the cutoff passes; just outside is filtered.
    Uses a 14-day cap with ±0.1 day from the boundary so we're nowhere
    near float-precision issues."""
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()

    now = datetime.now(UTC)
    session.add_all(
        [
            FeedEntry(
                feed_id=feed.id,
                guid="inside",
                title="inside",
                link="https://example.com/inside",
                published_at=now - timedelta(days=13.9),
            ),
            FeedEntry(
                feed_id=feed.id,
                guid="outside",
                title="outside",
                link="https://example.com/outside",
                published_at=now - timedelta(days=14.1),
            ),
        ]
    )
    sub = await _make_subscription(session, feed.id)
    repo = SubscriptionRepository(session)

    configure(max_entry_publish_age_days=14)
    unsent = await repo.get_unsent_entries_for_subscription(sub.id)

    guids = {e.guid for e in unsent}
    assert guids == {"inside"}


# ── sent records outlive their retention while the source lists the entry ────

_LONG_AGO = timedelta(days=100)


async def _sent_long_ago(session, guids: list[str], **feed_fields) -> Feed:
    """A subscribed feed whose `guids` were each sent 100 days ago."""
    feed = Feed(url=f"https://ex.com/{guids[0]}", is_active=True, error_count=0, **feed_fields)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="telegram", platform_user_id="u", platform_channel_id="c", feed_id=feed.id
    )
    session.add(sub)
    await session.flush()
    for guid in guids:
        session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=feed.id,
                guid=guid,
                sent_at=datetime.now(UTC) - _LONG_AGO,
            )
        )
    await session.commit()
    return feed


async def _remaining(session) -> list[str]:
    return sorted(await session.scalars(select(SentEntry.guid)))


async def test_record_survives_retention_while_the_source_still_lists_the_entry(session):
    """An undated entry passes every age gate, so a record dropped at 90 days while
    the source still lists the entry let it be delivered again, every 90 days."""
    feed = await _sent_long_ago(session, ["listed", "dropped"])
    svc = FeedService(session)
    svc.fetcher = AsyncMock()
    svc.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url=feed.url,
            success=True,
            entries=[{"guid": "listed", "title": "T", "link": "https://x/1"}],
        )
    )
    await svc.fetch_and_store(feed)
    await session.commit()

    await SubscriptionRepository(session).cleanup_old_sent_entries(90)
    await session.commit()

    assert await _remaining(session) == ["listed"]


async def test_record_survives_while_the_feed_only_answers_not_modified(session):
    # The last full snapshot predates the send, so nothing says the entry has left.
    await _sent_long_ago(
        session, ["static"], last_full_fetch_at=datetime.now(UTC) - _LONG_AGO - timedelta(days=1)
    )

    await SubscriptionRepository(session).cleanup_old_sent_entries(90)
    await session.commit()

    assert await _remaining(session) == ["static"]


async def test_record_of_a_feed_without_snapshots_ages_out(session):
    # Push sources are never fetched; their records go by when they were sent.
    await _sent_long_ago(session, ["pushed"])

    await SubscriptionRepository(session).cleanup_old_sent_entries(90)
    await session.commit()

    assert await _remaining(session) == []


async def _fetch_document(session, url: str, entries: list[dict]) -> Feed:
    """Store one fetched document for a new feed at `url`, listed as given (newest first)."""
    feed = Feed(url=url)
    session.add(feed)
    await session.flush()
    svc = FeedService(session)
    svc.fetcher = AsyncMock()
    svc.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(url=url, success=True, entries=entries)
    )
    await svc.fetch_and_store(feed)
    return feed


async def test_undated_entries_go_out_oldest_first_and_preview_the_newest(session):
    # Feeds list newest first; with no dates, storage order is all that tells them apart.
    feed = await _fetch_document(
        session,
        "https://example.com/undated",
        [{"guid": g, "title": g, "link": f"https://x/{g}"} for g in ("newest", "middle", "oldest")],
    )
    repo = SubscriptionRepository(session)
    delivered = await _make_subscription(session, feed.id)
    previewed = Subscription(
        platform="test", platform_user_id="u", platform_channel_id="chan-2", feed_id=feed.id
    )
    session.add(previewed)
    await session.flush()
    await repo.seed_sent_entries(previewed.id, feed.id, keep_latest=1)

    unsent = await repo.get_unsent_entries_for_subscription(delivered.id)
    preview = await repo.get_unsent_entries_for_subscription(previewed.id)

    assert [e.guid for e in unsent] == ["oldest", "middle", "newest"]
    assert [e.guid for e in preview] == ["newest"]


async def test_a_guid_listed_twice_keeps_its_newest_listing(session):
    feed = await _fetch_document(
        session,
        "https://example.com/dup",
        [
            {"guid": "g", "title": "revised", "link": "https://x/g"},
            {"guid": "h", "title": "other", "link": "https://x/h"},
            {"guid": "g", "title": "original", "link": "https://x/g"},
        ],
    )
    stored = await session.scalar(
        select(FeedEntry.title).where(FeedEntry.feed_id == feed.id, FeedEntry.guid == "g")
    )
    assert stored == "revised"
