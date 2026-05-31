"""Tests for the email/IMAP source: message→entry mapping (guid = Message-ID),
guid hash-fallback, config validation, password-from-env, and lazy
registration. The IMAP fetch is stubbed (no server); messages are built offline
with ``MailMessage.from_bytes``.
"""

from datetime import UTC

from imap_tools import MailMessage

from newsflow.core.source_fetcher import SourceRequest, get_source_fetcher
from newsflow.core.sources.email_imap import EmailSourceFetcher


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

    res = await f.fetch(
        SourceRequest(url="imap://me@host/INBOX", config=_ok_config())
    )

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

    res = await f.fetch(
        SourceRequest(url="imap://me@host/INBOX", config=_ok_config())
    )

    assert res.success
    guids = [e["guid"] for e in res.entries]
    assert guids[0] != guids[1]  # hash fallback stays distinct


async def test_missing_config_fails():
    res = await EmailSourceFetcher().fetch(
        SourceRequest(url="imap://x", config={"host": "h"})
    )
    assert res.success is False
    assert "user" in (res.error or "") and "password_env" in (res.error or "")


async def test_password_env_not_set_fails(monkeypatch):
    monkeypatch.delenv("NF_TEST_IMAP", raising=False)
    res = await EmailSourceFetcher().fetch(
        SourceRequest(url="imap://x", config=_ok_config())
    )
    assert res.success is False
    assert "NF_TEST_IMAP" in (res.error or "")  # names the missing env var


def test_email_registered_lazily():
    fetcher = get_source_fetcher("email_imap")
    assert fetcher is not None
    assert hasattr(fetcher, "fetch")
