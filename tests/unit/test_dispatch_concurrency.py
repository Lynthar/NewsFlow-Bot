"""Dispatch rounds must be serialised.

The loop used to be dispatch_once's only caller; ingest-triggered rounds
(push sources) now share the path. Two interleaved rounds would read the
same unsent entries before either marks them sent — a guaranteed
double-send — so overlapping calls must queue behind the mutex.
"""

import asyncio

from newsflow.services.dispatcher import Dispatcher, DispatchResult


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
