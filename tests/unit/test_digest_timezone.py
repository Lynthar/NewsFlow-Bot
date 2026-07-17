"""Timezone support for /digest enable: spelling parser, local→UTC
schedule conversion, and both platforms' argument surfaces.

The database keeps storing UTC only — conversion happens once at
configuration time (DST drift accepted by design)."""

from datetime import UTC, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from newsflow.adapters.discord.bot import DigestCommands
from newsflow.adapters.telegram.bot import _parse_digest_enable_args
from newsflow.core.timezones import local_schedule_to_utc, parse_timezone

# ===== parse_timezone =====


def test_parse_utc_and_gmt_spellings():
    assert parse_timezone("utc") == UTC
    assert parse_timezone("GMT") == UTC


def test_parse_iana_name():
    assert isinstance(parse_timezone("Asia/Shanghai"), ZoneInfo)


def test_parse_fixed_offsets():
    assert parse_timezone("+8") == timezone(timedelta(hours=8))
    assert parse_timezone("-5:30") == timezone(-timedelta(hours=5, minutes=30))
    assert parse_timezone("UTC+8") == timezone(timedelta(hours=8))
    assert parse_timezone("+08:00") == timezone(timedelta(hours=8))


def test_parse_rejects_unrecognized_spellings():
    # "8" (no sign) and "PST" (no region) stay rejected on purpose — the
    # Telegram tail-token sniffer must never eat a language code.
    for bad in ("", "abc", "8", "PST", "+15", "Mars/Base", "utc+"):
        assert parse_timezone(bad) is None, bad


# ===== local_schedule_to_utc =====


def test_daily_conversion_shanghai_evening():
    # 21:00 Asia/Shanghai (+8, no DST) = 13:00 UTC.
    assert local_schedule_to_utc(21, None, ZoneInfo("Asia/Shanghai")) == (13, None)


def test_weekly_conversion_shifts_weekday_across_midnight():
    # Monday 04:00 UTC+8 is Sunday 20:00 UTC — the pair converts jointly.
    assert local_schedule_to_utc(4, 0, timezone(timedelta(hours=8))) == (20, 6)


def test_utc_schedule_passes_through():
    assert local_schedule_to_utc(9, 2, UTC) == (9, 2)


# ===== Telegram /digest enable argument surface =====


def test_tg_enable_daily_defaults_to_utc_and_zh():
    assert _parse_digest_enable_args(["daily", "9"]) == ("daily", 9, None, "zh-CN", "UTC")


def test_tg_enable_tail_tokens_lang_and_tz_in_either_order():
    assert _parse_digest_enable_args(["daily", "21", "en", "Asia/Shanghai"]) == (
        "daily",
        21,
        None,
        "en",
        "Asia/Shanghai",
    )
    assert _parse_digest_enable_args(["daily", "21", "+8", "en"]) == (
        "daily",
        21,
        None,
        "en",
        "+8",
    )


def test_tg_enable_weekly_with_weekday_name_and_offset():
    assert _parse_digest_enable_args(["weekly", "mon", "4", "+8"]) == (
        "weekly",
        4,
        0,
        "zh-CN",
        "+8",
    )


def test_tg_enable_rejects_bad_hour_and_extra_tokens():
    with pytest.raises(ValueError):
        _parse_digest_enable_args(["daily", "25"])
    with pytest.raises(ValueError):
        _parse_digest_enable_args(["daily", "9", "en", "de", "fr"])
    with pytest.raises(ValueError):
        _parse_digest_enable_args(["daily", "9", "en", "de"])  # two languages


# ===== Discord /digest enable parameter surface =====


def test_discord_digest_enable_has_local_hour_plus_timezone():
    params = {p.name for p in DigestCommands.digest_enable.parameters}
    assert "hour" in params
    assert "timezone" in params
    assert "hour_utc" not in params
