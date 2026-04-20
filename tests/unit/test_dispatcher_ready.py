"""Tests for Dispatcher.wait_for_adapters — the startup-race guard."""

import asyncio
from unittest.mock import MagicMock, patch

from newsflow.services.dispatcher import Dispatcher


def _dispatcher_with(*, discord: bool, telegram: bool) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = discord
    fake.telegram_enabled = telegram
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


async def test_ready_immediately_when_no_platforms_enabled():
    dispatcher = _dispatcher_with(discord=False, telegram=False)

    assert await dispatcher.wait_for_adapters(timeout=0.1) is True


async def test_ready_after_all_expected_adapters_register():
    dispatcher = _dispatcher_with(discord=True, telegram=True)

    async def register_soon():
        await asyncio.sleep(0.02)
        dispatcher.register_adapter("discord", object())
        dispatcher.register_adapter("telegram", object())

    asyncio.create_task(register_soon())

    assert await dispatcher.wait_for_adapters(timeout=1.0) is True


async def test_times_out_if_adapter_never_registers():
    dispatcher = _dispatcher_with(discord=True, telegram=False)

    assert await dispatcher.wait_for_adapters(timeout=0.1) is False
