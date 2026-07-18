"""Regression test: a subscription's unsent backlog is flushed every dispatch
cycle, not only on cycles where some feed produced new entries.

A transient send failure deliberately leaves an entry unmarked so it retries.
Historically the per-subscription dispatch sat inside `if new_entries:`, so once
every feed went quiet (304 / no new items) the stranded entry was never retried
— and could age past the publish-age cutoff and vanish silently.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.services.dispatcher import Dispatcher


class _Ctx:
    """Reuse the fixture session inside dispatch_once's `async with`."""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        return False


async def test_backlog_delivered_when_cycle_has_no_new_entries(session, monkeypatch):
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()

    # An entry left unsent by a previous cycle (e.g. an earlier send failed
    # transiently). No SentEntry row exists for it yet.
    entry = FeedEntry(
        feed_id=feed.id,
        guid="stranded",
        title="Stranded article",
        link="https://example.com/stranded",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(entry)

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        silent=False,
        translate=False,
    )
    session.add(sub)
    await session.commit()

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory",
        lambda: lambda: _Ctx(session),
    )

    # This cycle's fetch produces NO new entries (304 Not Modified).
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[FetchResult(url=feed.url, success=True, entries=[], not_modified=True)]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    fake_settings = MagicMock()
    fake_settings.discord_enabled = True
    fake_settings.telegram_enabled = False
    fake_settings.webhooks_enabled = False
    fake_settings.fetch_interval_minutes = 60
    fake_settings.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake_settings):
        dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", adapter)

    with patch("newsflow.services.feed_service.get_settings", return_value=fake_settings):
        result = await dispatcher.dispatch_once()

    # No new entries surfaced this cycle...
    assert result.new_entries == 0
    # ...yet the stranded backlog entry was delivered and marked sent.
    adapter.send_message.assert_awaited_once()
    assert result.messages_sent == 1

    rows = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == sub.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].guid == "stranded"
