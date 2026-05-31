"""Tests for the P1 source-routing seam.

- ``Feed.source_type`` defaults to 'rss' and ``config`` round-trips as JSON.
- ``fetch_all_feeds`` routes non-RSS feeds to their registered SourceFetcher
  while RSS keeps its batch path; an unregistered type fails gracefully.

The routing must leave RSS behavior unchanged — with only RSS feeds present it
is the same single ``fetch_multiple`` call as before.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from newsflow.core import source_fetcher as sf
from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.services.feed_service import FeedService


async def test_feed_defaults_source_type_rss(session):
    f = Feed(url="https://ex.com/f")
    session.add(f)
    await session.commit()
    await session.refresh(f)
    assert f.source_type == "rss"
    assert f.config is None


async def test_feed_stores_config_json(session):
    f = Feed(
        url="https://ex.com/api",
        source_type="json_api",
        config={"items": "$.data[*]"},
    )
    session.add(f)
    await session.commit()
    await session.refresh(f)
    assert f.source_type == "json_api"
    assert f.config == {"items": "$.data[*]"}


async def test_fetch_all_feeds_routes_by_source_type(session, monkeypatch):
    rss = Feed(
        url="https://ex.com/rss", source_type="rss", is_active=True, error_count=0
    )
    api = Feed(
        url="https://ex.com/api",
        source_type="json_api",
        is_active=True,
        error_count=0,
        config={"k": "v"},
    )
    session.add_all([rss, api])
    await session.commit()

    svc = FeedService(session)
    # RSS still goes through the concurrent batch fetcher.
    svc.fetcher = SimpleNamespace(
        fetch_multiple=AsyncMock(
            return_value=[
                FetchResult(url=rss.url, success=True, entries=[], not_modified=True)
            ]
        )
    )

    seen = {}

    class _FakeJson:
        async def fetch(self, req):
            seen["req"] = req
            return FetchResult(url=req.url, success=True, entries=[], not_modified=True)

    monkeypatch.setitem(sf._REGISTRY, "json_api", _FakeJson())

    results = await svc.fetch_all_feeds()
    await session.commit()

    svc.fetcher.fetch_multiple.assert_awaited_once()  # RSS used the batch path
    assert seen["req"].url == "https://ex.com/api"  # json_api routed to fetcher
    assert seen["req"].config == {"k": "v"}  # config passed through
    assert len(results) == 2


async def test_unregistered_source_type_fails_gracefully(session):
    feed = Feed(
        url="https://ex.com/x", source_type="mystery", is_active=True, error_count=0
    )
    session.add(feed)
    await session.commit()

    svc = FeedService(session)
    # No fetcher registered for 'mystery' → graceful failure, not a crash, and
    # no attempt to batch-fetch it as RSS.
    results = await svc.fetch_all_feeds()
    await session.commit()

    assert len(results) == 1
    assert results[0].success is False


async def test_push_source_is_not_fetched(session):
    # webhook_inbound receives entries via the API, so the fetch loop must skip
    # it entirely — not route it to a (missing) fetcher and mark it errored.
    inbound = Feed(
        url="ci-events", source_type="webhook_inbound", is_active=True, error_count=0
    )
    session.add(inbound)
    await session.commit()

    svc = FeedService(session)
    svc.fetcher = SimpleNamespace(fetch_multiple=AsyncMock(return_value=[]))

    results = await svc.fetch_all_feeds()
    await session.commit()

    svc.fetcher.fetch_multiple.assert_not_awaited()  # no RSS batch either
    assert results == []  # the push source was skipped, nothing applied
    await session.refresh(inbound)
    assert inbound.error_count == 0  # crucially, NOT marked as a failed fetch
