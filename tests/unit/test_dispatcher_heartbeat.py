"""Tests for Dispatcher heartbeat — the liveness signal for HEALTHCHECK."""

import os
import time
from unittest.mock import MagicMock, patch

from newsflow.services.dispatcher import Dispatcher


def _dispatcher_with_data_dir(tmp_path) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.data_dir = tmp_path
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


def test_heartbeat_path_resolves_under_data_dir_heartbeat_subfolder(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)

    assert d.heartbeat_path("dispatch") == tmp_path / "heartbeat" / "dispatch"
    assert d.heartbeat_path("cleanup") == tmp_path / "heartbeat" / "cleanup"


def test_write_heartbeat_creates_named_file(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)

    d._write_heartbeat("dispatch")

    assert d.heartbeat_path("dispatch").exists()


def test_write_heartbeat_creates_missing_parent_dir(tmp_path):
    nested = tmp_path / "nested" / "data"
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.data_dir = nested
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        d = Dispatcher()

    d._write_heartbeat("dispatch")

    assert d.heartbeat_path("dispatch").exists()
    assert (nested / "heartbeat").is_dir()


def test_write_heartbeat_updates_mtime_on_existing_file(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)
    path = d.heartbeat_path("dispatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stale = time.time() - 3600
    os.utime(path, (stale, stale))

    d._write_heartbeat("dispatch")

    assert path.stat().st_mtime > stale + 100


def test_write_heartbeat_multiple_names_create_separate_files(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)

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
    d = _dispatcher_with_data_dir(tmp_path)
    with patch("pathlib.Path.mkdir", side_effect=OSError("readonly fs")):
        d._write_heartbeat("dispatch")  # must not raise


async def test_cleanup_loop_heartbeat_ticks_independently_of_cleanup_runs(tmp_path):
    """Heartbeat must update every `heartbeat_tick_seconds` even though
    the actual cleanup work only runs every `cleanup_interval_hours`.
    Without this, the 24h gap between cleanup runs would let the
    heartbeat go stale (>120 min), failing the Dockerfile HEALTHCHECK.

    Strategy: tick = 0.02s (~50/s), cleanup_interval = 1h (way longer
    than test runtime). Run the loop briefly under faked sleep, count
    cleanup calls vs heartbeat calls.
    """
    import asyncio

    fake_settings = MagicMock()
    fake_settings.discord_enabled = False
    fake_settings.telegram_enabled = False
    fake_settings.webhooks_enabled = False
    fake_settings.data_dir = tmp_path
    fake_settings.fetch_interval_minutes = 60
    fake_settings.cleanup_interval_hours = 1  # 3600s — cleanup won't re-fire
    fake_settings.entry_retention_days = 7
    fake_settings.sent_entry_retention_days = 90

    with patch("newsflow.services.dispatcher.get_settings", return_value=fake_settings):
        d = Dispatcher()

    # Skip the 60s startup delay — return immediately on the first sleep.
    real_sleep = asyncio.sleep
    sleep_calls = {"count": 0}

    async def fast_sleep(secs):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return  # initial 60s startup delay → instant
        await real_sleep(0)  # heartbeat ticks → yield control only

    # Mock the cleanup repo methods so we don't hit the DB.
    cleanup_calls = {"feed": 0, "sent": 0}

    class FakeFeedRepo:
        def __init__(self, session):
            pass

        async def cleanup_old_entries(self, days):
            cleanup_calls["feed"] += 1
            return 0

    class FakeSubRepo:
        def __init__(self, session):
            pass

        async def cleanup_old_sent_entries(self, days):
            cleanup_calls["sent"] += 1
            return 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    def fake_session_factory():
        return FakeSession()

    heartbeat_path = d.heartbeat_path("cleanup")

    with (
        patch("newsflow.services.dispatcher.asyncio.sleep", side_effect=fast_sleep),
        patch(
            "newsflow.services.dispatcher.get_session_factory", return_value=fake_session_factory
        ),
        patch("newsflow.services.dispatcher.FeedRepository", FakeFeedRepo),
        patch("newsflow.services.dispatcher.SubscriptionRepository", FakeSubRepo),
    ):
        task = asyncio.create_task(d.run_cleanup_loop(heartbeat_tick_seconds=0))
        # Yield control enough times for the loop to iterate several times.
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Cleanup ran exactly once: the first tick after startup. Subsequent
    # ticks see loop.time() < next_cleanup_at (interval_seconds=3600) and
    # skip the cleanup branch, only writing heartbeat.
    assert cleanup_calls["feed"] == 1
    assert cleanup_calls["sent"] == 1

    # Heartbeat file was created — written at least once.
    assert heartbeat_path.exists()

    # And several sleep ticks happened (proves the loop iterated past
    # the first cleanup run without re-firing it).
    assert sleep_calls["count"] >= 5
