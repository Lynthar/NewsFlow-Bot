"""Tests for the auto-deactivation notification path (C14)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.feed_service import FeedService
from tests import seed

DEAD_FEED = {
    "url": "https://example.com/feed",
    "title": "Dead Feed",
    "feed_fields": {"is_active": False},
}


def _adapter(send_text=None) -> MagicMock:
    adapter = MagicMock()
    adapter.send_text = send_text or AsyncMock(return_value=True)
    return adapter


async def test_apply_fetch_result_schedules_notify_on_deactivation(session, monkeypatch):
    """When the 10th error flips is_active False, a notify task is scheduled."""
    feed = Feed(
        url="https://example.com/feed",
        title="Dying Feed",
        is_active=True,
        error_count=9,  # one more error → deactivation
    )
    session.add(feed)
    await session.flush()

    scheduled: list[tuple] = []

    class StubDispatcher:
        async def notify_feed_deactivated(self, feed_id, url, title):
            scheduled.append((feed_id, url, title))

        def spawn(self, coro, *, name=None):
            return asyncio.create_task(coro, name=name)

    stub = StubDispatcher()
    monkeypatch.setattr("newsflow.services.dispatcher.get_dispatcher", lambda: stub)

    svc = FeedService(session)
    fr = FetchResult(url=feed.url, success=False, entries=[], error="HTTP 500")
    await svc._apply_fetch_result(feed, fr)
    # Yield so the create_task coroutine is allowed to run.
    await asyncio.sleep(0)

    assert feed.is_active is False
    assert scheduled == [(feed.id, feed.url, "Dying Feed")]


async def test_apply_fetch_result_does_not_notify_on_regular_error(session, monkeypatch):
    """Error 5 of 10 → still active → no notification scheduled."""
    feed = Feed(
        url="https://example.com/feed",
        title="Sick Feed",
        is_active=True,
        error_count=4,
    )
    session.add(feed)
    await session.flush()

    scheduled: list = []

    class StubDispatcher:
        async def notify_feed_deactivated(self, *args):
            scheduled.append(args)

        def spawn(self, coro, *, name=None):
            return asyncio.create_task(coro, name=name)

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_dispatcher",
        lambda: StubDispatcher(),
    )

    svc = FeedService(session)
    fr = FetchResult(url=feed.url, success=False, entries=[], error="HTTP 500")
    await svc._apply_fetch_result(feed, fr)
    await asyncio.sleep(0)

    assert feed.is_active is True
    assert scheduled == []


async def test_notify_feed_deactivated_sends_to_all_subscribers(db):
    """Notification reaches active AND paused subs across platforms."""
    # Active Discord sub + paused Telegram sub — both should get notified.
    active = await seed.subscription(
        db, platform="discord", channel_id="c-disc", user_id="u1", **DEAD_FEED
    )
    await seed.subscription(
        db, platform="telegram", channel_id="c-tg", user_id="u2", is_active=False, **DEAD_FEED
    )
    feed = active.feed

    discord_adapter, telegram_adapter = _adapter(), _adapter()
    d = Dispatcher()
    d.register_adapter("discord", discord_adapter)
    d.register_adapter("telegram", telegram_adapter)

    await d.notify_feed_deactivated(feed.id, feed.url, feed.title)

    discord_text = discord_adapter.send_text.call_args[0][1]
    telegram_text = telegram_adapter.send_text.call_args[0][1]
    assert discord_adapter.send_text.call_args[0][0] == "c-disc"
    assert telegram_adapter.send_text.call_args[0][0] == "c-tg"
    assert "/feed resume" in discord_text
    assert "/resume" in telegram_text
    assert "/feed resume" not in telegram_text


async def test_notify_feed_deactivated_swallows_adapter_errors(db):
    sub = await seed.subscription(db, platform="discord", channel_id="c", user_id="u", **DEAD_FEED)
    broken = _adapter(send_text=AsyncMock(side_effect=RuntimeError("api down")))
    d = Dispatcher()
    d.register_adapter("discord", broken)

    # Must not raise — a broken adapter shouldn't crash the notify path.
    await d.notify_feed_deactivated(sub.feed.id, sub.feed.url, sub.feed.title)
