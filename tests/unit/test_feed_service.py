"""Tests for FeedService._apply_fetch_result — the DB side of fetch.

We don't exercise the network path here; that belongs in an integration test.
"""

from newsflow.core.feed_fetcher import FetchResult
from newsflow.services.feed_service import FeedService


async def _make_feed(session, url: str = "https://example.com/feed"):
    from newsflow.repositories.feed_repository import FeedRepository

    repo = FeedRepository(session)
    return await repo.create_feed(url=url)


async def test_apply_fetch_result_stores_new_entries(session):
    feed = await _make_feed(session)
    svc = FeedService(session)
    fr = FetchResult(
        url=feed.url,
        success=True,
        entries=[
            {"guid": "a", "title": "A", "link": "https://x/a"},
            {"guid": "b", "title": "B", "link": "https://x/b"},
        ],
        etag="etag-1",
        last_modified="Wed, 21 Oct 2025 07:28:00 GMT",
        feed_title="Example",
    )

    result = await svc._apply_fetch_result(feed, fr)

    assert result.success is True
    assert {e.guid for e in result.new_entries} == {"a", "b"}
    # Metadata should be updated.
    await session.refresh(feed)
    assert feed.etag == "etag-1"
    assert feed.title == "Example"


async def test_apply_fetch_result_not_modified_keeps_old_entries(session):
    feed = await _make_feed(session)
    svc = FeedService(session)
    # Seed one existing entry.
    await svc._apply_fetch_result(
        feed,
        FetchResult(
            url=feed.url,
            success=True,
            entries=[{"guid": "existing", "title": "E", "link": "https://x/e"}],
        ),
    )

    fr = FetchResult(url=feed.url, success=True, entries=[], not_modified=True)
    result = await svc._apply_fetch_result(feed, fr)

    assert result.success is True
    assert result.message == "Not modified"
    assert result.new_entries == []


async def test_apply_fetch_result_failure_marks_error(session):
    feed = await _make_feed(session)
    svc = FeedService(session)
    fr = FetchResult(url=feed.url, success=False, entries=[], error="HTTP 500")

    result = await svc._apply_fetch_result(feed, fr)

    assert result.success is False
    assert "HTTP 500" in result.message
    # mark_feed_error mutates the feed via the session identity map,
    # so the same ORM instance reflects the change without a reload.
    assert feed.error_count == 1
    assert feed.last_error == "HTTP 500"


async def test_apply_fetch_result_empty_entries_returns_no_new(session):
    feed = await _make_feed(session)
    svc = FeedService(session)
    fr = FetchResult(url=feed.url, success=True, entries=[])

    result = await svc._apply_fetch_result(feed, fr)

    assert result.success is True
    assert result.message == "No new entries"
    assert result.new_entries == []


async def test_add_feed_reactivates_auto_disabled_existing(session):
    """Re-adding an existing feed that was auto-disabled revives it —
    otherwise remove + re-add (the notice's other suggestion) leaves the
    feed permanently dead."""
    feed = await _make_feed(session)
    feed.is_active = False
    feed.error_count = 10
    await session.flush()

    svc = FeedService(session)
    result = await svc.add_feed(feed.url)

    assert result.success is True
    assert result.feed.id == feed.id
    assert feed.is_active is True
    assert feed.error_count == 0


# ─── per-feed fetch interval (sources.yaml fetch_interval_minutes) ───────────


def test_feed_fetch_due_honors_per_feed_interval():
    from datetime import UTC, datetime, timedelta

    from newsflow.models.feed import Feed
    from newsflow.services.feed_service import _feed_fetch_due

    now = datetime.now(UTC)
    fresh = Feed(
        url="https://e/a",
        config={"fetch_interval_minutes": 240},
        last_fetched_at=now - timedelta(minutes=30),
    )
    elapsed = Feed(
        url="https://e/b",
        config={"fetch_interval_minutes": 240},
        last_fetched_at=now - timedelta(minutes=241),
    )
    plain = Feed(url="https://e/c", last_fetched_at=now - timedelta(minutes=1))
    never_fetched = Feed(url="https://e/d", config={"fetch_interval_minutes": 240})
    naive_ts = Feed(
        url="https://e/e",
        config={"fetch_interval_minutes": 240},
        # SQLite reads come back naive — must be treated as UTC, not crash.
        last_fetched_at=(now - timedelta(minutes=30)).replace(tzinfo=None),
    )
    malformed = Feed(
        url="https://e/f",
        config={"fetch_interval_minutes": "fast"},
        last_fetched_at=now - timedelta(minutes=1),
    )

    assert _feed_fetch_due(fresh, now) is False
    assert _feed_fetch_due(elapsed, now) is True
    assert _feed_fetch_due(plain, now) is True  # no key → global cadence
    assert _feed_fetch_due(never_fetched, now) is True
    assert _feed_fetch_due(naive_ts, now) is False
    assert _feed_fetch_due(malformed, now) is True  # fails open
