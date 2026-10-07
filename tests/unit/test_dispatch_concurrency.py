"""Dispatch rounds must be serialised.

The loop used to be dispatch_once's only caller; ingest-triggered rounds
(push sources) now share the path. Two interleaved rounds would read the
same unsent entries before either marks them sent — a guaranteed
double-send — so overlapping calls must queue behind the mutex.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from newsflow.core.feed_fetcher import FetchResult
from newsflow.services.dispatcher import Dispatcher, DispatchResult
from tests import seed


async def test_concurrent_dispatch_once_calls_never_overlap(monkeypatch):
    dispatcher = Dispatcher()
    active = 0
    overlapped = False

    async def instrumented_inner() -> DispatchResult:
        nonlocal active, overlapped
        active += 1
        if active > 1:
            overlapped = True
        await asyncio.sleep(0.02)
        active -= 1
        return DispatchResult()

    monkeypatch.setattr(dispatcher, "_dispatch_once_inner", instrumented_inner)

    await asyncio.gather(dispatcher.dispatch_once(), dispatcher.dispatch_once())

    assert overlapped is False


async def test_preview_and_a_round_never_send_the_same_entry(db, monkeypatch):
    """The /add preview runs the same per-subscription delivery as a round. While it
    is still sending, a round that reads the same unsent entry would push it again."""
    sub = await seed.subscription(db, platform="discord", channel_id="c", translate=False)
    await seed.entry(
        db, sub.feed_id, guid="latest", published_at=datetime.now(UTC) - timedelta(hours=1)
    )
    fetcher = MagicMock()
    fetcher.fetch_multiple = AsyncMock(
        return_value=[FetchResult(url=sub.feed.url, success=True, entries=[], not_modified=True)]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)

    sending = asyncio.Event()
    release = asyncio.Event()
    sends: list[str] = []

    async def send_message(channel_id, message):
        sends.append(message.link)
        if not sending.is_set():
            sending.set()
            await release.wait()
        return True

    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=send_message)
    adapter.is_connected = MagicMock(return_value=True)
    dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", adapter)

    preview = asyncio.create_task(dispatcher.schedule_preview(sub.id))
    await sending.wait()
    round_ = asyncio.create_task(dispatcher.dispatch_once())
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(preview, round_)

    assert len(sends) == 1
