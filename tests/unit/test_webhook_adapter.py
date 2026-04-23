"""Tests for WebhookAdapter._post — the HTTP sending logic.

We stub aiohttp with a lightweight fake so we can assert on the exact URL,
headers, and body that would be sent. This covers: destination lookup,
custom-header merging, HMAC signing, timeout handling, and HTTP error
paths — all without needing a real server.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import aiohttp

from newsflow.adapters.base import Message
from newsflow.adapters.webhook.bot import WebhookAdapter
from newsflow.adapters.webhook.formats import WireRequest
from newsflow.models.webhook import WebhookDestination


# ─── fake aiohttp session ────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.content = _FakeContent(body)

    async def __aenter__(self) -> "_FakeResponse":
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
        self, status: int = 200, body: bytes = b"", raise_exc: Exception | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url: str, **kwargs):  # noqa: ANN001 — matches aiohttp
        self.calls.append({"url": url, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResponse(self.status, self.body)

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


async def test_send_message_timeout_returns_false():
    session = _FakeSession(raise_exc=asyncio.TimeoutError())
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

    expected = hmac.new(
        secret.encode("utf-8"), sent["data"], hashlib.sha256
    ).hexdigest()
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
