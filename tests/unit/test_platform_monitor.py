"""Tests for Dispatcher.run_platform_monitor — per-platform heartbeats."""

import asyncio
from unittest.mock import MagicMock

from newsflow.services.dispatcher import Dispatcher


def _dispatcher(configure, *, discord=False, telegram=False) -> Dispatcher:
    # A platform is expected iff its token is configured; data_dir is the autouse tmp_path.
    configure(discord_token="d" if discord else None, telegram_token="t" if telegram else None)
    return Dispatcher()


class _FakeAdapter:
    def __init__(self, connected: bool):
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected

    async def send_message(self, channel_id, message):
        return True


async def test_platform_monitor_writes_heartbeat_for_connected_adapter(configure):
    d = _dispatcher(configure, discord=True)
    adapter = _FakeAdapter(connected=True)
    d.register_adapter("discord", adapter)

    # Run monitor for a short time then cancel
    task = asyncio.create_task(d.run_platform_monitor(interval_seconds=0.05))
    await asyncio.sleep(0.15)  # Let at least one iteration run
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert d.heartbeat_path("discord").exists()


async def test_platform_monitor_skips_disconnected_adapter(configure):
    d = _dispatcher(configure, discord=True)
    adapter = _FakeAdapter(connected=False)
    d.register_adapter("discord", adapter)

    task = asyncio.create_task(d.run_platform_monitor(interval_seconds=0.05))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not d.heartbeat_path("discord").exists()


async def test_platform_monitor_survives_is_connected_exceptions(configure):
    d = _dispatcher(configure, discord=True)
    adapter = MagicMock()
    adapter.is_connected = MagicMock(side_effect=RuntimeError("boom"))
    d.register_adapter("discord", adapter)

    task = asyncio.create_task(d.run_platform_monitor(interval_seconds=0.05))
    await asyncio.sleep(0.15)  # Should not raise
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # No crash; just no heartbeat written.
    assert not d.heartbeat_path("discord").exists()
