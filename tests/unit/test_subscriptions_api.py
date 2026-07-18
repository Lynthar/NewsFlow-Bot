"""Subscription management API: list / subscribe / pause / resume / delete /
OPML export, driven as direct route-function calls (no server)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")  # needs the api extra

from fastapi import HTTPException  # noqa: E402

from newsflow.api.routes.subscriptions import (  # noqa: E402
    SubscribeRequest,
    create_subscription,
    delete_subscription,
    export_opml,
    list_subscriptions,
    pause_subscription,
    resume_subscription,
)
from newsflow.core.feed_fetcher import FetchResult  # noqa: E402
from newsflow.models.feed import Feed  # noqa: E402
from newsflow.models.subscription import Subscription  # noqa: E402


async def _seed(session) -> tuple[Feed, Subscription, Subscription]:
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    active = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c1",
        feed_id=feed.id,
        is_active=True,
    )
    paused = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c1",
        feed_id=feed.id,
        is_active=False,
    )
    # Different feed rows can't share (platform, channel, feed) — use a
    # second feed for the paused sub.
    feed2 = Feed(url="https://example.com/feed2", title="Two", is_active=True, error_count=0)
    session.add(feed2)
    await session.flush()
    paused.feed_id = feed2.id
    session.add_all([active, paused])
    await session.commit()
    return feed, active, paused


async def test_list_includes_paused_and_maps_fields(session):
    feed, active, paused = await _seed(session)

    res = await list_subscriptions(platform="discord", channel="c1", db=session)

    assert res.total == 2
    by_id = {s.id: s for s in res.subscriptions}
    assert by_id[active.id].active is True
    assert by_id[paused.id].active is False
    assert by_id[active.id].feed_url == feed.url


async def test_pause_resume_delete_by_id(session):
    _feed, active, _paused = await _seed(session)

    res = await pause_subscription(sub_id=active.id, db=session, _=None)
    await session.commit()
    assert res.success is True
    await session.refresh(active)
    assert active.is_active is False

    res = await resume_subscription(sub_id=active.id, db=session, _=None)
    await session.commit()
    assert res.success is True
    await session.refresh(active)
    assert active.is_active is True

    res = await delete_subscription(sub_id=active.id, db=session, _=None)
    await session.commit()
    assert res.success is True

    with pytest.raises(HTTPException) as exc:
        await pause_subscription(sub_id=active.id, db=session, _=None)
    assert exc.value.status_code == 404


async def test_create_subscription_via_normal_add_path(session, monkeypatch):
    mock_fetcher = AsyncMock()
    mock_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="",
            success=True,
            entries=[{"guid": "e1", "title": "E1", "link": "https://n/e1"}],
            etag=None,
            last_modified=None,
            feed_title="New Feed",
            feed_description=None,
        )
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    res = await create_subscription(
        payload=SubscribeRequest(
            platform="telegram", channel_id="42", feed_url="https://new.example.com/rss"
        ),
        db=session,
        _=None,
    )
    await session.commit()

    assert res.success is True
    listed = await list_subscriptions(platform="telegram", channel="42", db=session)
    assert listed.total == 1
    assert listed.subscriptions[0].feed_url == "https://new.example.com/rss"


async def test_create_subscription_maps_add_failure_to_400(session, monkeypatch):
    mock_fetcher = AsyncMock()
    mock_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(url="", success=False, entries=[], error="HTTP 404")
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    with pytest.raises(HTTPException) as exc:
        await create_subscription(
            payload=SubscribeRequest(
                platform="telegram", channel_id="42", feed_url="https://dead.example.com/rss"
            ),
            db=session,
            _=None,
        )
    assert exc.value.status_code == 400


async def test_opml_export_contains_feed_urls(session):
    feed, _active, _paused = await _seed(session)

    response = await export_opml(platform="discord", channel="c1", db=session)

    body = response.body.decode()
    assert feed.url in body
    assert "<opml" in body
