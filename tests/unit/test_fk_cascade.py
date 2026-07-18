"""Regression tests for SQLite FK cascade enforcement.

Two cascade relationships matter for SentEntry:

1. Subscription -> SentEntry (ondelete=CASCADE): unsubscribing must
   wipe its SentEntry rows. Otherwise re-subscribing crashes on
   UNIQUE (subscription_id, feed_id, guid).

2. FeedEntry -> SentEntry: NO cascade since the 2026-05-08 migration.
   Cleanup of FeedEntry must NOT drop SentEntry — that's the whole
   point of switching to (feed_id, guid) as the natural key. SentEntry
   is the dedupe signal: re-ingestion of the same guid (a fresh
   FeedEntry row, possibly with a new id) must still be recognized as
   already-seen by dispatch.
"""

from unittest.mock import AsyncMock

from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.services.subscription_service import SubscriptionService


async def test_remove_then_readd_does_not_crash_on_orphan_sent_entries(session):
    """Subscription delete must cascade SentEntry, otherwise re-subscribe
    trips the UNIQUE (subscription_id, feed_id, guid) constraint."""
    svc = SubscriptionService(session)
    svc.feed_service.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="https://example.com/feed",
            success=True,
            entries=[
                {"guid": f"g{i}", "title": f"T{i}", "link": f"https://x/{i}"} for i in range(3)
            ],
            feed_title="Example",
        )
    )

    r1 = await svc.subscribe(
        platform="test",
        user_id="u1",
        channel_id="c1",
        feed_url="https://example.com/feed",
    )
    await session.commit()
    assert r1.success and r1.is_new
    sub1_id = r1.subscription.id

    # Seeded 2 of 3 (one kept for preview).
    sent = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == sub1_id)))
        .scalars()
        .all()
    )
    assert len(sent) == 2

    # Unsubscribe.
    u1 = await svc.unsubscribe(
        platform="test",
        channel_id="c1",
        feed_url="https://example.com/feed",
    )
    await session.commit()
    assert u1.success

    # FK cascade should have cleared the SentEntry rows.
    orphans = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == sub1_id)))
        .scalars()
        .all()
    )
    assert orphans == [], "FK cascade did not fire — stale SentEntry rows remain"

    # Re-subscribe: must not raise IntegrityError.
    r2 = await svc.subscribe(
        platform="test",
        user_id="u1",
        channel_id="c1",
        feed_url="https://example.com/feed",
    )
    await session.commit()
    assert r2.success and r2.is_new


async def test_feed_entry_delete_does_not_cascade_to_sent_entry(session):
    """The 2026-05-08 schema deliberately drops the FK from SentEntry
    to FeedEntry. Cleanup of a FeedEntry must leave SentEntry intact —
    that's the dedupe signal that prevents re-delivery on re-ingestion.

    Without this guarantee the cleanup-then-rediscover bug returns.
    """
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()

    entry = FeedEntry(
        feed_id=feed.id,
        guid="dedupe-guid",
        title="Article",
        link="https://example.com/a",
    )
    session.add(entry)
    await session.flush()

    sub = Subscription(
        platform="test",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    # Mark the entry sent — this is the dedupe signal.
    sent = SentEntry(
        subscription_id=sub.id,
        feed_id=feed.id,
        guid="dedupe-guid",
        was_filtered=False,
    )
    session.add(sent)
    await session.flush()

    # Delete the FeedEntry (simulates cleanup_old_entries deleting an
    # aged-out row). With the old FK + CASCADE this would also blow
    # away the SentEntry; under the new schema it must NOT.
    await session.delete(entry)
    await session.flush()

    survivors = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == sub.id)))
        .scalars()
        .all()
    )
    assert len(survivors) == 1
    assert survivors[0].guid == "dedupe-guid"
    assert survivors[0].feed_id == feed.id
