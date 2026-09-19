"""Tests for the feeds API refresh route.

A successful manual refresh is proof the source works — it must revive an
auto-disabled feed (the F9 recovery-contract family; the dispatch loop skips
inactive feeds, so nothing else could clear the state).

Route functions are called directly with Depends defaults overridden, like
test_ingest_api.py — no server, no HTTP: the fetch is stubbed at the boundary.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastapi")  # needs the api extra

from newsflow.api.routes.feeds import refresh_feed  # noqa: E402
from newsflow.core.feed_fetcher import FetchResult  # noqa: E402
from newsflow.models.feed import Feed  # noqa: E402


def _source_is_fine(monkeypatch, url: str) -> None:
    """The feed answers 304: reachable, nothing new."""
    fetcher = MagicMock()
    fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(url=url, success=True, entries=[], not_modified=True)
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)


async def test_refresh_success_reactivates_auto_disabled_feed(session, monkeypatch):
    feed = Feed(
        url="https://example.com/rss",
        title="t",
        is_active=False,
        error_count=10,
    )
    session.add(feed)
    await session.commit()
    _source_is_fine(monkeypatch, feed.url)

    await refresh_feed(feed.id, db=session, _=None)

    # The route mutates in memory; get_db commits when the request completes.
    assert feed.is_active is True
    assert feed.error_count == 0
    await session.commit()
    refreshed = await session.get(Feed, feed.id)
    assert refreshed is not None and refreshed.is_active is True


async def test_refresh_success_leaves_active_feed_alone(session, monkeypatch):
    feed = Feed(url="https://example.com/rss", title="t", is_active=True)
    session.add(feed)
    await session.commit()
    _source_is_fine(monkeypatch, feed.url)

    await refresh_feed(feed.id, db=session, _=None)

    assert feed.is_active is True
