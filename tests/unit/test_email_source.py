"""Tests for the email/IMAP source: message→entry mapping (guid = Message-ID),
guid hash-fallback, config validation, password-from-env, and lazy
registration. The IMAP fetch is stubbed (no server); messages are built offline
with ``MailMessage.from_bytes``.
"""

import ssl
from datetime import UTC

import pytest

from newsflow.core.source_fetcher import SourceRequest, get_source_fetcher
from newsflow.core.sources.email_imap import EmailSourceFetcher

# Skip the whole module if the source-email extra isn't installed.
MailMessage = pytest.importorskip("imap_tools").MailMessage


def _email(
    message_id: str | None = "<m1@example.com>",
    subject: str = "Weekly News",
    frm: str = "news@example.com",
    body: str = "Hello body",
    date: str = "Sat, 31 May 2026 08:00:00 +0000",
) -> MailMessage:
    lines = []
    if message_id:
        lines.append(f"Message-ID: {message_id}")
    lines += [
        f"From: {frm}",
        f"Subject: {subject}",
        f"Date: {date}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
        "",
    ]
    return MailMessage.from_bytes("\r\n".join(lines).encode())


def _ok_config() -> dict:
    return {
        "host": "imap.example.com",
        "user": "me@example.com",
        "password_env": "NF_TEST_IMAP",
    }


async def test_maps_message_to_entry(monkeypatch):
    f = EmailSourceFetcher()
    monkeypatch.setenv("NF_TEST_IMAP", "app-password")
    monkeypatch.setattr(f, "_fetch_sync", lambda *a, **k: [_email()])

    res = await f.fetch(SourceRequest(url="imap://me@host/INBOX", config=_ok_config()))

    assert res.success
    e = res.entries[0]
    assert e["guid"] == "<m1@example.com>"  # guid = Message-ID
    assert e["title"] == "Weekly News"
    assert e["summary"].startswith("Hello body")
    assert e["author"] == "news@example.com"
    assert e["link"] == "imap://me@host/INBOX"
    assert e["published_at"].tzinfo is UTC


async def test_guid_falls_back_when_no_message_id(monkeypatch):
    f = EmailSourceFetcher()
    monkeypatch.setenv("NF_TEST_IMAP", "x")
    monkeypatch.setattr(
        f,
        "_fetch_sync",
        lambda *a, **k: [
            _email(message_id=None, subject="A"),
            _email(message_id=None, subject="B"),
        ],
    )

    res = await f.fetch(SourceRequest(url="imap://me@host/INBOX", config=_ok_config()))

    assert res.success
    guids = [e["guid"] for e in res.entries]
    assert guids[0] != guids[1]  # hash fallback stays distinct


async def test_missing_config_fails():
    res = await EmailSourceFetcher().fetch(SourceRequest(url="imap://x", config={"host": "h"}))
    assert res.success is False
    assert "user" in (res.error or "") and "password_env" in (res.error or "")


async def test_password_env_not_set_fails(monkeypatch):
    monkeypatch.delenv("NF_TEST_IMAP", raising=False)
    res = await EmailSourceFetcher().fetch(SourceRequest(url="imap://x", config=_ok_config()))
    assert res.success is False
    assert "NF_TEST_IMAP" in (res.error or "")  # names the missing env var


def test_email_registered_lazily():
    fetcher = get_source_fetcher("email_imap")
    assert fetcher is not None
    assert hasattr(fetcher, "fetch")


# ─── undated mail ────────────────────────────────────────────────────────────


def _email_without_date() -> MailMessage:
    raw = "\r\n".join(
        [
            "Message-ID: <undated@example.com>",
            "From: news@example.com",
            "Subject: No Date header",
            "Content-Type: text/plain; charset=utf-8",
            "",
            "body",
            "",
        ]
    )
    return MailMessage.from_bytes(raw.encode())


async def test_undated_mail_has_no_published_at(monkeypatch):
    """imap-tools reports 1900-01-01 when Date is missing. Passing that through
    puts the entry outside max_entry_publish_age_days, so it would be filtered
    out of every dispatch and never reach the subscriber."""
    f = EmailSourceFetcher()
    monkeypatch.setenv("NF_TEST_IMAP", "x")
    monkeypatch.setattr(f, "_fetch_sync", lambda *a, **k: [_email_without_date()])

    res = await f.fetch(SourceRequest(url="imap://me@host/INBOX", config=_ok_config()))

    assert res.success
    assert res.entries[0]["published_at"] is None


async def test_dated_mail_still_maps_its_date(monkeypatch):
    f = EmailSourceFetcher()
    monkeypatch.setenv("NF_TEST_IMAP", "x")
    monkeypatch.setattr(f, "_fetch_sync", lambda *a, **k: [_email()])

    res = await f.fetch(SourceRequest(url="imap://me@host/INBOX", config=_ok_config()))

    assert res.entries[0]["published_at"] is not None


# ─── TLS ─────────────────────────────────────────────────────────────────────


class _CapturingMailBox:
    """Records the ssl_context and timeout the fetcher hands to imap_tools."""

    seen: dict = {}

    def __init__(self, host, port, timeout=None, ssl_context=None):
        _CapturingMailBox.seen.update(ssl_context=ssl_context, timeout=timeout)

    def login(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch(self, **kwargs):
        return []


def _context_for(tls: str) -> ssl.SSLContext:
    _CapturingMailBox.seen = {}
    EmailSourceFetcher._fetch_sync(
        _CapturingMailBox, "imap.example.com", 993, "u", "p", "INBOX", 10, tls
    )
    return _CapturingMailBox.seen["ssl_context"]


def test_connection_has_a_socket_timeout():
    # Without one, a server that accepts and then stops answering wedges the round.
    _context_for("verify")
    assert 0 < _CapturingMailBox.seen["timeout"] <= 60


def test_default_tls_verifies_the_certificate():
    """Without an explicit context imaplib verifies nothing, which hands the
    mailbox password to anyone able to intercept the connection."""
    ctx = _context_for("verify")
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_insecure_tls_opts_out():
    ctx = _context_for("insecure")
    assert ctx.verify_mode is ssl.CERT_NONE
    assert ctx.check_hostname is False


async def test_unknown_tls_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("NF_TEST_IMAP", "x")
    config = _ok_config() | {"tls": "yolo"}
    res = await EmailSourceFetcher().fetch(SourceRequest(url="imap://x", config=config))
    assert res.success is False
    assert "tls" in (res.error or "") and "yolo" in (res.error or "")
