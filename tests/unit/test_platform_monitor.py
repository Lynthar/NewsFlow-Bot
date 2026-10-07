"""Tests for Dispatcher.run_platform_monitor — per-platform heartbeats."""

import asyncio
import json
from unittest.mock import MagicMock

from telegram.ext import Application
from telegram.request import BaseRequest

from newsflow.adapters.discord.bot import DiscordAdapter, NewsFlowBot
from newsflow.adapters.telegram.bot import TelegramAdapter
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


# ===== what each adapter reports as connected =====


async def test_discord_reports_disconnected_while_the_gateway_is_down():
    # discord.py keeps is_ready() True through a reconnect, so readiness can't tell.
    bot = NewsFlowBot()
    adapter = DiscordAdapter(bot)

    await bot.on_connect()
    assert adapter.is_connected() is True
    await bot.on_disconnect()
    assert adapter.is_connected() is False
    await bot.on_resumed()
    assert adapter.is_connected() is True


class _BotApi(BaseRequest):
    """The Bot API as it answers once the token is revoked mid-run: start-up calls
    succeeded, and getUpdates now answers 401."""

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        if url.endswith("/getUpdates"):
            return 401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}'
        result = {"id": 1, "is_bot": True, "first_name": "b", "username": "b"}
        if url.endswith("/deleteWebhook"):
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def test_telegram_reports_disconnected_once_polling_has_died():
    # PTB ends the polling task on InvalidToken but leaves updater.running True.
    api = _BotApi()
    app = Application.builder().token("1:x").request(api).get_updates_request(api).build()
    adapter = TelegramAdapter(token="1:x")
    adapter.app = app
    await app.initialize()
    assert app.updater is not None
    await app.updater.start_polling()
    try:
        for _ in range(100):
            if not adapter.is_connected():
                break
            await asyncio.sleep(0.01)
        assert app.updater.running is True
        assert adapter.is_connected() is False
    finally:
        await adapter.stop()
