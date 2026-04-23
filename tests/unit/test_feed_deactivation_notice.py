"""Tests for the auto-deactivation notification path (C14)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.models.subscription import Subscription
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.feed_service import FeedService


def _dispatcher_with_adapter(platform: str, adapter) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = platform == "discord"
    fake.telegram_enabled = platform == "telegram"
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    # data_dir only needed for heartbeat — not used in notification path.
    fake.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        d = Dispatcher()
    d.register_adapter(platform, adapter)
    return d


async def test_apply_fetch_result_schedules_notify_on_deactivation(session, monkeypatch):
    """When the 10th error flips is_active False, a notify task is scheduled."""
    feed = Feed(
        url="https://example.com/feed",
        title="Dying Feed",
        is_active=True,
        error_count=9,
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
    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_dispatcher", lambda: stub
    )

    svc = FeedService(session)
    fr = FetchResult(
        url=feed.url, success=False, entries=[], error="HTTP 500"
    )
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


async def test_notify_feed_deactivated_sends_to_all_subscribers(session, monkeypatch):
    """Notification reaches active AND paused subs across platforms."""
    feed = Feed(url="https://example.com/feed", title="Dead Feed", is_active=False)
    session.add(feed)
    await session.flush()
    # Active Discord sub + paused Telegram sub — both should get notified.
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u1",
            platform_channel_id="c-disc",
            feed_id=feed.id,
            is_active=True,
        )
    )
    session.add(
        Subscription(
            platform="telegram",
            platform_user_id="u2",
            platform_channel_id="c-tg",
            feed_id=feed.id,
            is_active=False,
        )
    )
    await session.commit()

    # Patch get_session_factory so notify_feed_deactivated uses our session.
    factory_called = MagicMock()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    def _factory():
        factory_called()
        return _Ctx()

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory",
        lambda: _factory,
    )

    discord_adapter = MagicMock()
    discord_adapter.send_text = AsyncMock(return_value=True)
    telegram_adapter = MagicMock()
    telegram_adapter.send_text = AsyncMock(return_value=True)

    d = _dispatcher_with_adapter("discord", discord_adapter)
    d.register_adapter("telegram", telegram_adapter)

    await d.notify_feed_deactivated(feed.id, feed.url, feed.title)

    discord_adapter.send_text.assert_awaited_once()
    telegram_adapter.send_text.assert_awaited_once()
    # Verify channel IDs routed correctly.
    assert discord_adapter.send_text.call_args[0][0] == "c-disc"
    assert telegram_adapter.send_text.call_args[0][0] == "c-tg"


async def test_notify_feed_deactivated_swallows_adapter_errors(session, monkeypatch):
    feed = Feed(url="https://example.com/feed", title="Dead Feed", is_active=False)
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

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory", lambda: _Ctx,
    )

    broken = MagicMock()
    broken.send_text = AsyncMock(side_effect=RuntimeError("api down"))
    d = _dispatcher_with_adapter("discord", broken)

    # Must not raise — a broken adapter shouldn't crash the notify path.
    await d.notify_feed_deactivated(feed.id, feed.url, feed.title)
