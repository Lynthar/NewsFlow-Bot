"""Tests for Dispatcher.wait_for_adapters — the startup-race guard."""

import asyncio

from newsflow.services.dispatcher import Dispatcher


def _dispatcher_with(configure, *, discord: bool, telegram: bool) -> Dispatcher:
    # A platform is expected iff its token is configured.
    configure(discord_token="d" if discord else None, telegram_token="t" if telegram else None)
    return Dispatcher()


async def test_ready_immediately_when_no_platforms_enabled(configure):
    dispatcher = _dispatcher_with(configure, discord=False, telegram=False)

    assert await dispatcher.wait_for_adapters(timeout=0.1) is True


async def test_ready_after_all_expected_adapters_register(configure):
    dispatcher = _dispatcher_with(configure, discord=True, telegram=True)

    async def register_soon():
        await asyncio.sleep(0.02)
        dispatcher.register_adapter("discord", object())
        dispatcher.register_adapter("telegram", object())

    asyncio.create_task(register_soon())

    assert await dispatcher.wait_for_adapters(timeout=1.0) is True


async def test_times_out_if_adapter_never_registers(configure):
    dispatcher = _dispatcher_with(configure, discord=True, telegram=False)

    assert await dispatcher.wait_for_adapters(timeout=0.1) is False
