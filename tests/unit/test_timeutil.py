"""Tests for core/timeutil relative/until helpers."""

from datetime import datetime, timedelta, timezone

from newsflow.core.timeutil import relative_time, time_until


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _ahead(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def test_relative_time_none_returns_never():
    assert relative_time(None) == "never"


def test_relative_time_seconds_is_just_now():
    assert relative_time(_ago(30)) == "just now"


def test_relative_time_minutes():
    assert relative_time(_ago(5 * 60)) == "5m ago"


def test_relative_time_hours():
    assert relative_time(_ago(3 * 3600)) == "3h ago"


def test_relative_time_days():
    assert relative_time(_ago(2 * 86400)) == "2d ago"


def test_relative_time_future_collapses_to_just_now():
    assert relative_time(_ahead(10)) == "just now"


def test_relative_time_accepts_naive_datetime():
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    assert relative_time(naive) in {"5m ago", "4m ago"}  # allow 1m drift


def test_time_until_none_returns_never():
    assert time_until(None) == "never"


def test_time_until_past_is_now():
    assert time_until(_ago(10)) == "now"


def test_time_until_soon():
    assert time_until(_ahead(30)) == "soon"


def test_time_until_minutes():
    # +2s buffer so μs of elapsed time between _ahead() and time_until()
    # don't floor-divide our 15m target down to 14m.
    assert time_until(_ahead(15 * 60 + 2)) == "in 15m"


def test_time_until_hours():
    assert time_until(_ahead(4 * 3600 + 2)) == "in 4h"


def test_time_until_days():
    assert time_until(_ahead(3 * 86400 + 2)) == "in 3d"
