"""Feeds API routes: a successful refresh revives an auto-disabled feed (the
dispatch loop skips inactive ones), and /test goes through HTTP so its request
validation is exercised. The fetch is stubbed at the network boundary."""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastapi")  # needs the api extra
pytest.importorskip("httpx")  # drives the app over ASGI; not an api-extra dependency

import httpx  # noqa: E402

from newsflow.api import create_app  # noqa: E402
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


def _fetcher_answers(monkeypatch) -> AsyncMock:
    fetch = AsyncMock(side_effect=lambda url: FetchResult(url=url, success=True, entries=[]))
    monkeypatch.setattr("newsflow.core.get_fetcher", lambda: MagicMock(fetch_feed=fetch))
    return fetch


async def _post_test(configure, url: str) -> httpx.Response:
    configure(api_key="secret")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        return await client.post(
            "/api/feeds/test", json={"url": url}, headers={"Authorization": "Bearer secret"}
        )


@pytest.mark.parametrize(
    ("given", "fetched"),
    [
        ("gh:owner/repo", "https://github.com/owner/repo/releases.atom"),
        ("pypi:pytest", "https://pypi.org/rss/project/pytest/releases.xml"),
        ("https://example.com/feed", "https://example.com/feed"),
    ],
)
async def test_test_route_expands_shortcuts_before_validating(
    configure, monkeypatch, given, fetched
):
    fetch = _fetcher_answers(monkeypatch)

    response = await _post_test(configure, given)

    assert response.status_code == 200, response.text
    fetch.assert_awaited_once_with(fetched)


async def test_test_route_rejects_what_does_not_expand_to_a_url(configure, monkeypatch):
    fetch = _fetcher_answers(monkeypatch)

    response = await _post_test(configure, "gh:owner-without-repo")

    assert response.status_code == 422
    fetch.assert_not_awaited()
