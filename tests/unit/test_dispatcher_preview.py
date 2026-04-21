"""Tests for the fire-and-forget schedule_preview path."""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.services.dispatcher import Dispatcher


def _dispatcher() -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


async def test_schedule_preview_swallows_exceptions():
    """A failed preview must never bubble up — it's fire-and-forget."""
    d = _dispatcher()

    async def boom(_id):
        raise RuntimeError("pretend the DB is on fire")

    with patch.object(d, "dispatch_subscription", side_effect=boom):
        # Must not raise.
        await d.schedule_preview(123)


async def test_schedule_preview_calls_dispatch_subscription():
    d = _dispatcher()
    mock = AsyncMock(return_value=1)
    with patch.object(d, "dispatch_subscription", mock):
        await d.schedule_preview(42)
    mock.assert_awaited_once_with(42)
