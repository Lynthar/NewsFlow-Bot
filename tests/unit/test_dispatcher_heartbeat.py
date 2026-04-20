"""Tests for Dispatcher heartbeat — the liveness signal for HEALTHCHECK."""

import os
import time
from unittest.mock import MagicMock, patch

from newsflow.services.dispatcher import Dispatcher


def _dispatcher_with_data_dir(tmp_path) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.data_dir = tmp_path
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


def test_heartbeat_path_resolves_under_data_dir(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)

    assert d.heartbeat_path == tmp_path / ".heartbeat"


def test_write_heartbeat_creates_file(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)

    d._write_heartbeat()

    assert d.heartbeat_path.exists()


def test_write_heartbeat_creates_missing_parent_dir(tmp_path):
    nested = tmp_path / "nested" / "data"
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.data_dir = nested
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        d = Dispatcher()

    d._write_heartbeat()

    assert d.heartbeat_path.exists()
    assert nested.is_dir()


def test_write_heartbeat_updates_mtime_on_existing_file(tmp_path):
    d = _dispatcher_with_data_dir(tmp_path)
    d.heartbeat_path.touch()
    stale = time.time() - 3600
    os.utime(d.heartbeat_path, (stale, stale))

    d._write_heartbeat()

    assert d.heartbeat_path.stat().st_mtime > stale + 100


def test_write_heartbeat_swallows_filesystem_errors(tmp_path):
    """A failed heartbeat must never break dispatch."""
    d = _dispatcher_with_data_dir(tmp_path)
    with patch("pathlib.Path.mkdir", side_effect=OSError("readonly fs")):
        d._write_heartbeat()  # must not raise
