"""Regression test for SQLite FK cascade enforcement.

Without `PRAGMA foreign_keys=ON`, deleting a Subscription leaves orphan
SentEntry rows behind, and re-subscribing triggers a UNIQUE constraint
crash on (subscription_id, entry_id) because the old rows still exist.
"""

from unittest.mock import AsyncMock

from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.subscription import SentEntry
from newsflow.services.subscription_service import SubscriptionService


async def test_remove_then_readd_does_not_crash_on_orphan_sent_entries(session):
    svc = SubscriptionService(session)
    svc.feed_service.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="https://example.com/feed",
            success=True,
            entries=[
                {"guid": f"g{i}", "title": f"T{i}", "link": f"https://x/{i}"}
                for i in range(3)
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
        await session.execute(
            select(SentEntry).where(SentEntry.subscription_id == sub1_id)
        )
    ).scalars().all()
    assert len(sent) == 2

    # Unsubscribe.
    u1 = await svc.unsubscribe(
        platform="test",
        channel_id="c1",
        feed_url="https://example.com/feed",
    )
    await session.commit()
    assert u1.success

    # FK cascade should have cleared the SentEntry rows. Without
    # PRAGMA foreign_keys=ON these would linger and the next subscribe
    # would crash with UNIQUE constraint failure.
    orphans = (
        await session.execute(
            select(SentEntry).where(SentEntry.subscription_id == sub1_id)
        )
    ).scalars().all()
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
