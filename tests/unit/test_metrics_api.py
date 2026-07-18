"""/metrics endpoint + backlog surfacing in the status detail."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # needs the api extra

from newsflow.api.routes.metrics import metrics  # noqa: E402
from newsflow.models.feed import Feed, FeedEntry  # noqa: E402
from newsflow.models.subscription import SentEntry, Subscription  # noqa: E402
from newsflow.services.subscription_service import SubscriptionService  # noqa: E402


class _FakeDispatcher:
    class totals:  # noqa: N801 — attribute container, mirrors DispatcherTotals
        dispatch_rounds = 3
        feeds_fetched = 12
        new_entries = 7
        messages_sent = 5
        send_errors = 1


async def test_metrics_renders_prometheus_text(session, monkeypatch):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id="c",
            feed_id=feed.id,
            is_active=True,
        )
    )
    await session.commit()
    monkeypatch.setattr("newsflow.api.routes.metrics.get_dispatcher", lambda: _FakeDispatcher())

    response = await metrics(db=session)

    body = response.body.decode()
    assert "text/plain" in response.media_type
    assert "# TYPE newsflow_dispatch_rounds_total counter" in body
    assert "newsflow_dispatch_rounds_total 3" in body
    assert "newsflow_messages_sent_total 5" in body
    assert "newsflow_feeds 1" in body
    assert "newsflow_subscriptions_active 1" in body


async def test_status_detail_reports_backlog(session):
    feed = Feed(url="https://example.com/feed", title="Ex", is_active=True, error_count=0)
    session.add(feed)
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
    # Three entries, one already sent → backlog of 2.
    for guid in ("a", "b", "c"):
        session.add(
            FeedEntry(feed_id=feed.id, guid=guid, title=guid.upper(), link=f"https://x/{guid}")
        )
    session.add(SentEntry(subscription_id=sub.id, feed_id=feed.id, guid="a"))
    await session.commit()

    detail = await SubscriptionService(session).get_subscription_detail(
        platform="discord", channel_id="c", feed_url=feed.url
    )

    assert detail is not None
    assert detail.unsent_count == 2
