"""Settings that the lint cleanup left dead are now wired through.

Covers LOG_FORMAT (json vs console rendering, including exception
tracebacks from both stdlib and structlog loggers) and FEED_MAX_CONCURRENT
(get_fetcher reading the setting instead of a hardcoded 10).
"""

import io
import json
import logging
import sys

import pytest
import structlog
from pydantic import ValidationError

import newsflow.core.feed_fetcher as feed_fetcher
from newsflow.config import Settings
from newsflow.main import setup_logging


def _format_record(msg: str = "hello world") -> str:
    """Format one plain stdlib record through the root handler's formatter."""
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, None, None)
    return handler.format(record)


def _format_exception_record(msg: str = "it broke") -> str:
    """Format a stdlib record carrying exc_info, as logger.exception() would."""
    handler = logging.getLogger().handlers[0]
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, msg, None, sys.exc_info())
    return handler.format(record)


def test_log_format_json_emits_parseable_json() -> None:
    setup_logging(Settings(_env_file=None, log_format="json"))
    data = json.loads(_format_record())  # raises if not JSON
    assert data["event"] == "hello world"
    assert data["level"] == "info"
    assert "timestamp" in data


def test_log_format_console_is_not_json() -> None:
    setup_logging(Settings(_env_file=None, log_format="console"))
    out = _format_record()
    assert "hello world" in out
    try:
        json.loads(out)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("console output should not be valid JSON")


def test_json_stdlib_exception_renders_traceback() -> None:
    """logger.exception() from a stdlib logger must yield a real formatted
    traceback in json mode — not repr(exc_info) with a '<traceback object'
    placeholder (which is what JSONRenderer emits without format_exc_info)."""
    setup_logging(Settings(_env_file=None, log_format="json"))
    data = json.loads(_format_exception_record())
    assert data["event"] == "it broke"
    assert "Traceback (most recent call last)" in data["exception"]
    assert "ValueError: boom" in data["exception"]
    assert "traceback object" not in json.dumps(data)


def test_json_structlog_exception_renders_traceback() -> None:
    """structlog's .exception() (exc_info=True) must also come out with the
    formatted stack, resolved while the except block is still active."""
    setup_logging(Settings(_env_file=None, log_format="json"))
    buf = io.StringIO()
    logging.getLogger().handlers[0].setStream(buf)
    # Fresh logger name: cache_logger_on_first_use=True would otherwise hand
    # back a logger bound to a previous test's processor chain.
    slog = structlog.get_logger("test_json_structlog_exception")
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        slog.exception("structlog failure")
    data = json.loads(buf.getvalue().strip())
    assert data["event"] == "structlog failure"
    assert "Traceback (most recent call last)" in data["exception"]
    assert "RuntimeError: kaboom" in data["exception"]


def test_httpx_quieted_to_warning() -> None:
    """PTB routes Bot API calls through httpx, whose INFO request line
    contains the bot token in the URL path. setup_logging must keep
    httpx/httpcore at WARNING so the token never reaches the logs."""
    setup_logging(Settings(_env_file=None))
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_console_exception_still_renders_traceback() -> None:
    """format_exc_info hands ConsoleRenderer a pre-formatted 'exception'
    string; the traceback must still be visible in console mode."""
    setup_logging(Settings(_env_file=None, log_format="console"))
    out = _format_exception_record()
    assert "Traceback (most recent call last)" in out
    assert "ValueError: boom" in out


def test_feed_max_concurrent_rejects_zero() -> None:
    """0 would build an asyncio.Semaphore(0) — every fetch blocks forever
    while the bot looks alive. Must fail loudly at startup instead."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, feed_max_concurrent=0)


def test_get_fetcher_reads_max_concurrent_from_settings(configure) -> None:
    configure(feed_max_concurrent=25)
    feed_fetcher._fetcher = None
    try:
        fetcher = feed_fetcher.get_fetcher()
        assert fetcher.max_concurrent == 25
        assert fetcher._semaphore._value == 25
    finally:
        feed_fetcher._fetcher = None


# ===== value bounds (negative/zero intervals used to pass silently) =====


@pytest.mark.parametrize(
    "field",
    [
        "cleanup_interval_hours",
        "digest_check_interval_minutes",
        "translation_cache_ttl_days",
        "digest_max_input_chars_per_article",
    ],
)
def test_non_positive_intervals_are_rejected(field):
    """A zero/negative interval turns the corresponding sleep-loop into a
    busy spin — reject at load so checkconfig catches it pre-deploy."""
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(telegram_token="x", **{field: bad})
    assert getattr(Settings(telegram_token="x", **{field: 1}), field) == 1


def test_api_port_range_enforced():
    for bad in (-1, 0, 65536):
        with pytest.raises(ValidationError):
            Settings(telegram_token="x", api_port=bad)
    assert Settings(telegram_token="x", api_port=8000).api_port == 8000


def test_max_feeds_per_channel_rejects_negative():
    with pytest.raises(ValidationError):
        Settings(telegram_token="x", max_feeds_per_channel=-1)
    assert Settings(telegram_token="x", max_feeds_per_channel=0).max_feeds_per_channel == 0


# ===== webhook-only deployments =====


def test_minimal_config_accepts_webhooks_yaml_without_tokens(tmp_path):
    """A headless RSS→webhook pipeline is a complete deployment: the file's
    presence satisfies the minimal-config gate with no chat token set."""
    yaml_path = tmp_path / "webhooks.yaml"
    no_platform = Settings(webhooks_config_path=yaml_path)
    assert no_platform.validate_minimal_config() is False  # file absent

    yaml_path.write_text("destinations: {}\n", encoding="utf-8")
    webhook_only = Settings(webhooks_config_path=yaml_path)
    assert webhook_only.validate_minimal_config() is True
