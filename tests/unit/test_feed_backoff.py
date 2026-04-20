"""Tests for exponential backoff on feed fetch errors."""

from datetime import datetime, timedelta, timezone

from newsflow.models.feed import Feed
from newsflow.repositories.feed_repository import FeedRepository


def test_mark_error_sets_next_retry_with_doubling():
    """Delay doubles per consecutive error."""
    feed = Feed(url="https://example.com/feed", error_count=0)
    base = 60  # 1 minute base for easy math

    feed.mark_error("boom", base_delay_seconds=base)
    first = feed.next_retry_at
    # First error: factor = 2^1 = 2, so delay ≈ 120s
    assert first is not None

    feed.mark_error("boom", base_delay_seconds=base)
    second = feed.next_retry_at
    # Second error: factor = 2^2 = 4, delay ≈ 240s → second - now > first - now
    assert (second - first).total_seconds() > 0


def test_mark_error_caps_backoff_factor():
    """Factor caps at 2^5 = 32 regardless of error_count."""
    feed = Feed(url="https://example.com/feed")
    base = 100

    # Drive error_count up to 7 without deactivating
    feed.error_count = 6  # next call → 7
    feed.is_active = True
    before = datetime.now(timezone.utc)
    feed.mark_error("boom", base_delay_seconds=base)
    delay_at_7 = (feed.next_retry_at - before).total_seconds()

    # factor at error_count=7 is still min(7,5)=5 → 2^5 * 100 = 3200s
    assert 3100 < delay_at_7 < 3300


def test_mark_error_deactivates_at_ten():
    feed = Feed(
        url="https://example.com/feed", is_active=True, error_count=9
    )
    feed.mark_error("final")
    assert feed.is_active is False
    assert feed.error_count == 10


def test_mark_success_clears_backoff():
    feed = Feed(url="https://example.com/feed", error_count=0)
    feed.mark_error("boom", base_delay_seconds=60)
    assert feed.next_retry_at is not None
    assert feed.error_count == 1

    feed.mark_success(etag="new-etag")

    assert feed.next_retry_at is None
    assert feed.error_count == 0
    assert feed.last_error is None


async def test_get_feeds_due_for_fetch_excludes_backoff(session):
    repo = FeedRepository(session)
    ok = await repo.create_feed(url="https://example.com/ok")
    backed_off = await repo.create_feed(url="https://example.com/slow")
    backed_off.next_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await session.flush()

    due = await repo.get_feeds_due_for_fetch()

    assert {f.url for f in due} == {ok.url}


async def test_get_feeds_due_for_fetch_includes_expired_backoff(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/recovered")
    feed.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()

    due = await repo.get_feeds_due_for_fetch()

    assert [f.url for f in due] == [feed.url]


async def test_update_feed_metadata_clears_next_retry(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    feed.next_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    feed.error_count = 3
    feed.last_error = "old error"
    await session.flush()

    await repo.update_feed_metadata(feed.id, etag="new")

    await session.refresh(feed)
    assert feed.next_retry_at is None
    assert feed.error_count == 0
    assert feed.last_error is None
