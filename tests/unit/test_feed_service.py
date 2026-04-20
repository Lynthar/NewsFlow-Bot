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
    fr = FetchResult(
        url=feed.url, success=False, entries=[], error="HTTP 500"
    )

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
