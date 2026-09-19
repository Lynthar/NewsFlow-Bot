"""Tests for Dispatcher heartbeat — the liveness signal for HEALTHCHECK.

``data_dir`` is the autouse ``tmp_path``: it derives from the database URL."""

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry
from newsflow.services.dispatcher import Dispatcher
from tests import seed


def test_heartbeat_path_resolves_under_data_dir_heartbeat_subfolder(tmp_path):
    d = Dispatcher()

    assert d.heartbeat_path("dispatch") == tmp_path / "heartbeat" / "dispatch"
    assert d.heartbeat_path("cleanup") == tmp_path / "heartbeat" / "cleanup"


def test_write_heartbeat_creates_named_file():
    d = Dispatcher()

    d._write_heartbeat("dispatch")

    assert d.heartbeat_path("dispatch").exists()


def test_write_heartbeat_creates_missing_parent_dir(configure, tmp_path):
    nested = tmp_path / "nested" / "data"
    configure(database_url=f"sqlite+aiosqlite:///{nested / 'newsflow.db'}")
    d = Dispatcher()

    d._write_heartbeat("dispatch")

    assert d.heartbeat_path("dispatch").exists()
    assert (nested / "heartbeat").is_dir()


def test_write_heartbeat_updates_mtime_on_existing_file():
    d = Dispatcher()
    path = d.heartbeat_path("dispatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stale = time.time() - 3600
    os.utime(path, (stale, stale))

    d._write_heartbeat("dispatch")

    assert path.stat().st_mtime > stale + 100


def test_write_heartbeat_multiple_names_create_separate_files():
    d = Dispatcher()

    d._write_heartbeat("dispatch")
    d._write_heartbeat("cleanup")
    d._write_heartbeat("discord")

    assert d.heartbeat_path("dispatch").exists()
    assert d.heartbeat_path("cleanup").exists()
    assert d.heartbeat_path("discord").exists()
    # And they must be distinct files.
    assert d.heartbeat_path("dispatch") != d.heartbeat_path("cleanup")


def test_write_heartbeat_swallows_filesystem_errors(tmp_path):
    """A failed heartbeat must never break dispatch."""
    d = Dispatcher()
    (tmp_path / "heartbeat").write_text("")  # a file sits where the directory must go

    d._write_heartbeat("dispatch")  # must not raise

    assert not d.heartbeat_path("dispatch").exists()


async def test_cleanup_loop_heartbeat_ticks_independently_of_cleanup_runs(db, configure):
    """Heartbeat must update every `heartbeat_tick_seconds` even though
    the actual cleanup work only runs every `cleanup_interval_hours`.
    Without this, the 24h gap between cleanup runs would let the
    heartbeat go stale (>120 min), failing the Dockerfile HEALTHCHECK.

    Strategy: tick = 0, cleanup_interval = 1h (way longer than the test).
    The first tick runs cleanup and deletes an over-age entry and an over-age
    sent record; ones inserted after that survive every later tick, which
    only touch the heartbeat.
    """
    configure(cleanup_interval_hours=1)
    d = Dispatcher()
    ancient = datetime.now(UTC) - timedelta(days=400)
    sub = await seed.subscription(db, url="https://ex.com/feed")

    async def add_old_entry(guid: str) -> None:
        async with db() as session:
            feed = await session.scalar(select(Feed)) or Feed(url="https://ex.com/feed")
            session.add(feed)
            await session.flush()
            session.add(
                FeedEntry(
                    feed_id=feed.id,
                    guid=guid,
                    title=guid,
                    link=f"https://ex.com/{guid}",
                    created_at=ancient,
                )
            )
            await session.commit()

    async def add_old_sent(guid: str) -> None:
        async with db() as session:
            session.add(
                SentEntry(subscription_id=sub.id, feed_id=sub.feed_id, guid=guid, sent_at=ancient)
            )
            await session.commit()

    async def remaining() -> list[str]:
        async with db() as session:
            return list(await session.scalars(select(FeedEntry.guid)))

    async def remaining_sent() -> list[str]:
        async with db() as session:
            return list(await session.scalars(select(SentEntry.guid)))

    await add_old_entry("first")
    await add_old_sent("first")

    # Skip the hard-coded 60s startup delay; every later sleep just yields.
    real_sleep = asyncio.sleep
    ticks = 0

    async def fast_sleep(secs):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            await real_sleep(0)

    with patch("newsflow.services.dispatcher.asyncio.sleep", side_effect=fast_sleep):
        task = asyncio.create_task(d.run_cleanup_loop(heartbeat_tick_seconds=0))
        for _ in range(200):  # the cleanup's database round-trips take real time
            if await remaining() == [] and await remaining_sent() == []:
                break
            await real_sleep(0.01)
        assert await remaining() == []  # the first tick ran cleanup
        assert await remaining_sent() == []

        await add_old_entry("second")
        await add_old_sent("second")
        ticks_then = ticks
        for _ in range(200):  # let several heartbeat ticks go by
            if ticks >= ticks_then + 5:
                break
            await real_sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert ticks >= ticks_then + 5
    assert await remaining() == ["second"]  # no second cleanup within the interval
    assert await remaining_sent() == ["second"]
    assert d.heartbeat_path("cleanup").exists()


def test_clear_stale_heartbeats_removes_previous_runs_files(tmp_path):
    """Heartbeats live in the persistent data volume. A platform disabled
    between runs leaves its old file behind, and HEALTHCHECK flags ANY
    stale file — the container would go permanently unhealthy. Startup
    must sweep the directory."""
    from newsflow.config import get_settings
    from newsflow.main import clear_stale_heartbeats

    hb = tmp_path / "heartbeat"
    hb.mkdir()
    (hb / "discord").touch()
    (hb / "dispatch").touch()

    clear_stale_heartbeats(get_settings())

    assert list(hb.iterdir()) == []


def test_clear_stale_heartbeats_tolerates_missing_dir(configure, tmp_path):
    from newsflow.main import clear_stale_heartbeats

    settings = configure(database_url=f"sqlite+aiosqlite:///{tmp_path / 'nonexistent' / 'x.db'}")
    clear_stale_heartbeats(settings)  # must not raise
