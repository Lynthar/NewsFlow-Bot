"""Regression test: a subscription's unsent backlog is flushed every dispatch
cycle, not only on cycles where some feed produced new entries.

A transient send failure deliberately leaves an entry unmarked so it retries.
Historically the per-subscription dispatch sat inside `if new_entries:`, so once
every feed went quiet (304 / no new items) the stranded entry was never retried
— and could age past the publish-age cutoff and vanish silently.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.subscription import SentEntry
from newsflow.services.dispatcher import Dispatcher
from tests import seed


async def test_backlog_delivered_when_cycle_has_no_new_entries(db, monkeypatch):
    sub = await seed.subscription(
        db,
        platform="discord",
        channel_id="c",
        user_id="u",
        url="https://example.com/feed",
        title="Example",
        translate=False,
    )
    # An entry left unsent by a previous cycle (e.g. an earlier send failed
    # transiently). No SentEntry row exists for it yet.
    await seed.entry(
        db,
        sub.feed_id,
        guid="stranded",
        title="Stranded article",
        link="https://example.com/stranded",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )

    # This cycle's fetch produces NO new entries (304 Not Modified).
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[FetchResult(url=sub.feed.url, success=True, entries=[], not_modified=True)]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", adapter)

    result = await dispatcher.dispatch_once()

    # No new entries surfaced this cycle...
    assert result.new_entries == 0
    # ...yet the stranded backlog entry was delivered and marked sent.
    adapter.send_message.assert_awaited_once()
    assert result.messages_sent == 1

    async with db() as session:
        rows = list(
            await session.scalars(select(SentEntry).where(SentEntry.subscription_id == sub.id))
        )
    assert [row.guid for row in rows] == ["stranded"]
