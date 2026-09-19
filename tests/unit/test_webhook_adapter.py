"""Tests for WebhookAdapter._post — the HTTP sending logic.

We stub aiohttp with a lightweight fake so we can assert on the exact URL,
headers, and body that would be sent. This covers: destination lookup,
custom-header merging, HMAC signing, timeout handling, and HTTP error
paths — all without needing a real server.
"""

from __future__ import annotations

import hashlib
import hmac

import aiohttp
import pytest_asyncio

from newsflow.adapters.base import Message
from newsflow.adapters.webhook.bot import WebhookAdapter
from newsflow.models.webhook import WebhookDestination


@pytest_asyncio.fixture
async def session(db):
    """A session on the shared test database; the adapter opens its own for accounting."""
    async with db() as s:
        yield s


# ─── fake aiohttp session ────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n >= 0 else self._body


class _FakeSession:
    """Records post() calls and returns canned responses. Drop-in for
    aiohttp.ClientSession inside WebhookAdapter tests."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        raise_exc: Exception | None = None,
        statuses: list[int] | None = None,
        headers: dict | None = None,
    ) -> None:
        # `statuses` drives retry tests: one entry per post(), the last one repeating.
        self.statuses = statuses or [status]
        self.body = body
        self.raise_exc = raise_exc
        self.headers = headers or {}
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url: str, **kwargs):  # noqa: ANN001 — matches aiohttp
        self.calls.append({"url": url, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        status = self.statuses[min(len(self.calls), len(self.statuses)) - 1]
        return _FakeResponse(status, self.body, self.headers)

    async def close(self) -> None:
        self.closed = True


def _make_adapter(session: _FakeSession | None = None) -> WebhookAdapter:
    a = WebhookAdapter()
    a._session = session or _FakeSession()  # type: ignore[assignment]
    a._started = True
    return a


def _dest(**overrides) -> WebhookDestination:
    defaults = dict(
        name="test",
        url="https://example.com/webhook",
        format="generic",
        secret=None,
        headers=None,
        timeout_s=10,
    )
    defaults.update(overrides)
    return WebhookDestination(**defaults)


def _message() -> Message:
    return Message(
        title="Hello",
        summary="summary",
        link="https://example.com/a",
        source="Source",
    )


# ─── basic success / failure paths ───────────────────────────────────────────


async def test_send_message_unknown_destination_returns_false():
    adapter = _make_adapter()
    ok = await adapter.send_message("not-configured", _message())
    assert ok is False


async def test_send_message_success_returns_true():
    session = _FakeSession(status=200)
    adapter = _make_adapter(session)
    adapter._destinations = {"slack": _dest(name="slack", format="slack")}

    ok = await adapter.send_message("slack", _message())

    assert ok is True
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://example.com/webhook"
    # Slack's fallback text should be in the body
    assert b"Hello" in session.calls[0]["data"]


async def test_send_message_http_error_returns_false():
    session = _FakeSession(status=500, body=b"internal server error")
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    ok = await adapter.send_message("x", _message())

    assert ok is False


async def test_send_message_value_error_returns_false():
    """A header-validation ValueError (illegal header bytes) is a failed send,
    not an uncaught exception that would wedge the entry in the dispatch loop."""
    session = _FakeSession(raise_exc=ValueError("Invalid header value"))
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    ok = await adapter.send_message("x", _message())

    assert ok is False


async def test_send_message_timeout_returns_false():
    session = _FakeSession(raise_exc=TimeoutError())
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x", timeout_s=1)}

    ok = await adapter.send_message("x", _message())

    assert ok is False


async def test_send_message_client_error_returns_false():
    session = _FakeSession(raise_exc=aiohttp.ClientError("boom"))
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    ok = await adapter.send_message("x", _message())

    assert ok is False


# ─── header merging + HMAC signing ───────────────────────────────────────────


async def test_custom_headers_are_merged():
    session = _FakeSession(status=204)
    adapter = _make_adapter(session)
    adapter._destinations = {
        "x": _dest(name="x", headers={"Authorization": "Bearer abc", "X-Route": "news"})
    }

    await adapter.send_message("x", _message())

    sent = session.calls[0]["headers"]
    assert sent["Authorization"] == "Bearer abc"
    assert sent["X-Route"] == "news"
    # format defaults still present
    assert sent["Content-Type"].startswith("application/json")


async def test_hmac_signature_when_secret_present():
    session = _FakeSession(status=200)
    adapter = _make_adapter(session)
    secret = "my-hmac-key"
    adapter._destinations = {"x": _dest(name="x", secret=secret)}

    await adapter.send_message("x", _message())

    sent = session.calls[0]
    sig_header = sent["headers"]["X-NewsFlow-Signature"]
    assert sig_header.startswith("sha256=")

    expected = hmac.new(secret.encode("utf-8"), sent["data"], hashlib.sha256).hexdigest()
    assert sig_header == f"sha256={expected}"


async def test_no_signature_header_when_secret_missing():
    session = _FakeSession(status=200)
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x", secret=None)}

    await adapter.send_message("x", _message())

    sent = session.calls[0]["headers"]
    assert "X-NewsFlow-Signature" not in sent


async def test_send_text_uses_notification_converter():
    """System notifications (e.g. feed auto-disabled) should go out as the
    format's text/notification payload, not the entry-shaped payload."""
    session = _FakeSession(status=200)
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x", format="generic")}

    await adapter.send_text("x", "a feed was auto-disabled")

    import json

    body = json.loads(session.calls[0]["data"])
    assert body["event"] == "system.notification"
    assert body["text"] == "a feed was auto-disabled"


async def test_timeout_value_from_destination_is_used():
    session = _FakeSession(status=200)
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x", timeout_s=3)}

    await adapter.send_message("x", _message())

    timeout = session.calls[0]["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total == 3


# ─── circuit breaker ─────────────────────────────────────────────────────────


async def _persisted_dest(session, **overrides) -> WebhookDestination:
    defaults = dict(name="brk", url="https://example.com/webhook", format="generic")
    defaults.update(overrides)
    dest = WebhookDestination(**defaults)
    session.add(dest)
    await session.commit()
    return dest


async def test_breaker_trips_after_ten_straight_failures(session):
    dest = await _persisted_dest(session)
    fake = _FakeSession(status=500, body=b"boom")
    adapter = _make_adapter(fake)
    adapter._destinations = {"brk": dest}

    for _ in range(10):
        assert await adapter.send_message("brk", _message()) is False

    assert dest.is_active is False
    assert dest.error_count == 10
    assert "HTTP 500" in (dest.last_error or "")

    # Breaker open: the next send short-circuits without touching the network.
    assert await adapter.send_message("brk", _message()) is False
    assert len(fake.calls) == 10


async def test_success_resets_the_failure_counter(session):
    dest = await _persisted_dest(session, error_count=7, last_error="HTTP 500")
    adapter = _make_adapter(_FakeSession(status=200))
    adapter._destinations = {"brk": dest}

    assert await adapter.send_message("brk", _message()) is True

    assert dest.error_count == 0
    assert dest.last_error is None
    assert dest.is_active is True


async def test_transient_destination_skips_accounting():
    # Unit-style destinations that were never persisted (id=None) must not
    # open DB sessions from the accounting path.
    adapter = _make_adapter(_FakeSession(status=500))
    adapter._destinations = {"x": _dest(name="x")}
    assert await adapter.send_message("x", _message()) is False  # no crash, no DB


async def test_post_does_not_follow_redirects():
    """A webhook endpoint answering POST with a 30x is a misconfigured
    destination; following it would replay the signed body + custom auth
    headers to an unvetted URL. The adapter must post with
    allow_redirects=False and count the 30x as a failed send."""
    session = _FakeSession(status=302)
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    ok = await adapter.send_message("x", _message())

    assert ok is False
    assert session.calls[0]["allow_redirects"] is False


# ─── rate limiting ───────────────────────────────────────────────────────────


def _patch_sleep(monkeypatch) -> list[float]:
    """Record the back-off waits instead of really sleeping."""
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("newsflow.adapters.webhook.bot.asyncio.sleep", _fake_sleep)
    return slept


async def test_rate_limited_send_retries_once_then_succeeds(monkeypatch):
    slept = _patch_sleep(monkeypatch)
    session = _FakeSession(statuses=[429, 204], headers={"Retry-After": "1.5"})
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    ok = await adapter.send_message("x", _message())

    assert ok is True
    assert len(session.calls) == 2
    assert slept == [1.5]


async def test_discord_rate_limit_prefers_reset_after(monkeypatch):
    """Verified against a live Discord webhook: its Retry-After is in
    MILLISECONDS, so reading that as seconds would defer every rate-limited
    entry to the next round instead of retrying two seconds later."""
    slept = _patch_sleep(monkeypatch)
    session = _FakeSession(
        statuses=[429, 204],
        headers={"Retry-After": "1922", "X-RateLimit-Reset-After": "2"},
    )
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    assert await adapter.send_message("x", _message()) is True
    assert slept == [2.0]


async def test_rate_limit_without_retry_after_still_retries(monkeypatch):
    """Retry-After may legally be an HTTP-date. An unparseable value falls back
    to a short wait rather than counting the send as failed."""
    slept = _patch_sleep(monkeypatch)
    session = _FakeSession(
        statuses=[429, 204], headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    assert await adapter.send_message("x", _message()) is True
    assert slept == [1.0]


async def test_rate_limit_over_the_cap_defers_without_waiting(monkeypatch):
    """Dispatch is serial, so a long Retry-After must not stall every other
    destination — the entry waits for the next round instead."""
    slept = _patch_sleep(monkeypatch)
    session = _FakeSession(status=429, headers={"Retry-After": "600"})
    adapter = _make_adapter(session)
    adapter._destinations = {"x": _dest(name="x")}

    assert await adapter.send_message("x", _message()) is False
    assert len(session.calls) == 1
    assert slept == []


async def test_rate_limit_never_credits_the_breaker(session, monkeypatch):
    """A 429 is the receiver pacing us, not a broken endpoint. Counting them
    would disable a healthy destination after ten, stopping delivery outright."""
    _patch_sleep(monkeypatch)
    dest = await _persisted_dest(session)
    fake = _FakeSession(status=429, headers={"Retry-After": "1"})
    adapter = _make_adapter(fake)
    adapter._destinations = {"brk": dest}

    for _ in range(10):
        assert await adapter.send_message("brk", _message()) is False

    assert dest.is_active is True
    assert not dest.error_count


async def test_rate_limit_does_not_clear_earlier_failures(session, monkeypatch):
    """The mirror of the rule above: a 429 must not reset a real failure streak
    either, or an endpoint that alternates 500s and 429s never trips."""
    _patch_sleep(monkeypatch)
    dest = await _persisted_dest(session, error_count=7, last_error="HTTP 500")
    adapter = _make_adapter(_FakeSession(status=429, headers={"Retry-After": "1"}))
    adapter._destinations = {"brk": dest}

    assert await adapter.send_message("brk", _message()) is False

    assert dest.error_count == 7
    assert dest.last_error == "HTTP 500"
