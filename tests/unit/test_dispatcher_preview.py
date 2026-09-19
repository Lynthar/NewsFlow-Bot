"""Tests for the fire-and-forget schedule_preview path."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from newsflow.models.base import close_db
from newsflow.services.dispatcher import Dispatcher
from tests import seed


async def test_schedule_preview_swallows_exceptions(db, configure, tmp_path):
    """A failed preview must never bubble up — it's fire-and-forget."""
    d = Dispatcher()
    # The database goes away underneath it: a file that cannot be opened.
    await close_db()
    configure(database_url=f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'x.db'}")

    await d.schedule_preview(123)  # must not raise


async def test_schedule_preview_delivers_the_subscription_backlog(db):
    sub = await seed.subscription(db, platform="discord", channel_id="c", translate=False)
    await seed.entry(
        db,
        sub.feed_id,
        guid="latest",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    d = Dispatcher()
    d.register_adapter("discord", adapter)

    await d.schedule_preview(sub.id)

    adapter.send_message.assert_awaited_once()
    assert adapter.send_message.await_args.args[0] == "c"
