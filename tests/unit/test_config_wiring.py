"""Settings that the lint cleanup left dead are now wired through.

Covers LOG_FORMAT (json vs console rendering) and FEED_MAX_CONCURRENT
(get_fetcher reading the setting instead of a hardcoded 10).
"""

import json
import logging
from types import SimpleNamespace

import newsflow.core.feed_fetcher as feed_fetcher
from newsflow.config import Settings
from newsflow.main import setup_logging


def _format_record(msg: str = "hello world") -> str:
    """Format one plain stdlib record through the root handler's formatter."""
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, None, None)
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


def test_get_fetcher_reads_max_concurrent_from_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        feed_fetcher, "get_settings", lambda: SimpleNamespace(feed_max_concurrent=25)
    )
    feed_fetcher._fetcher = None
    try:
        fetcher = feed_fetcher.get_fetcher()
        assert fetcher.max_concurrent == 25
        assert fetcher._semaphore._value == 25
    finally:
        feed_fetcher._fetcher = None
