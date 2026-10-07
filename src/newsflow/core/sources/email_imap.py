"""Email source: poll an IMAP mailbox and map messages to entries — ideal for
newsletters that have no RSS. Optional dependency: ``imap-tools`` (extra
``source-email``).

``Feed.config`` shape (``Feed.url`` is just an identifier, e.g.
``imap://user@host/INBOX``; the connection uses config, not the URL):

    host:          IMAP server hostname                              (required)
    user:          login user                                        (required)
    password_env:  NAME of the env var holding the password          (required)
    port:          IMAP SSL port (default 993)
    tls:           "verify" (default) or "insecure"; "insecure" accepts any
                   certificate, exposing the password to an active MITM
    mailbox:       folder to read (default "INBOX")
    limit:         max newest messages to fetch per poll (default 50)

The password is read from the named environment variable and **never stored in
the database**. Use an app-specific password, not your main account password.
``guid`` is the email's Message-ID, so dedupe is exact across polls (re-fetching
the newest N each cycle is harmless — already-sent messages are skipped).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import ssl
from datetime import UTC, datetime
from typing import Any

from newsflow.core.feed_fetcher import FetchResult
from newsflow.core.source_fetcher import SourceRequest, register_source_fetcher

logger = logging.getLogger(__name__)

_TLS_MODES = frozenset({"verify", "insecure"})

# Bounds every socket read, not just the connect: a server that accepts and then goes
# silent would otherwise hold the worker thread, and the dispatch round, forever.
_SOCKET_TIMEOUT_S = 30

# imap-tools hands back this value when the Date header is missing or unparseable.
_NO_DATE = datetime(1900, 1, 1)


def _fail(url: str, error: str) -> FetchResult:
    return FetchResult(url=url, success=False, entries=[], error=error)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class EmailSourceFetcher:
    """Fetch recent messages from an IMAP mailbox as entries."""

    async def fetch(self, req: SourceRequest) -> FetchResult:
        config = req.config or {}
        host = config.get("host")
        user = config.get("user")
        password_env = config.get("password_env")
        if not host or not user or not password_env:
            missing = [
                name
                for name, val in (
                    ("host", host),
                    ("user", user),
                    ("password_env", password_env),
                )
                if not val
            ]
            return _fail(req.url, f"email: config missing {', '.join(missing)}")

        # The password lives in the environment, never in the DB.
        password = os.environ.get(password_env)
        if not password:
            return _fail(req.url, f"email: env var {password_env!r} is not set")

        try:
            from imap_tools import MailBox
        except ImportError:
            return _fail(
                req.url,
                "email source needs the 'source-email' extra (pip install imap-tools)",
            )

        port = _int(config.get("port"), 993)
        mailbox = config.get("mailbox") or "INBOX"
        limit = _int(config.get("limit"), 50)
        tls = str(config.get("tls") or "verify").lower()
        if tls not in _TLS_MODES:
            return _fail(
                req.url, f"email: config.tls must be one of {sorted(_TLS_MODES)}, got {tls!r}"
            )

        try:
            messages = await asyncio.to_thread(
                self._fetch_sync, MailBox, host, port, user, password, mailbox, limit, tls
            )
        except ssl.SSLCertVerificationError as e:
            # Certificates went unchecked before this knob existed, so a self-hosted
            # server that used to work now fails and the operator needs the way out.
            return _fail(
                req.url,
                f"{type(e).__name__}: {e} — add `tls: insecure` to this source's config "
                "to accept it anyway (that exposes the password to an active MITM)",
            )
        except Exception as e:
            # Connection/login/protocol failure — surface without the password.
            return _fail(req.url, f"{type(e).__name__}: {e}")

        entries = [self._message_to_entry(m, req.url) for m in messages]
        return FetchResult(url=req.url, success=True, entries=entries)

    @staticmethod
    def _fetch_sync(
        mailbox_cls: Any,
        host: str,
        port: int,
        user: str,
        password: str,
        mailbox: str,
        limit: int,
        tls: str,
    ) -> list[Any]:
        """Blocking IMAP fetch, run in a worker thread. Newest first and
        read-only (mark_seen=False) — Message-ID dedupe makes re-fetching the
        same window each cycle harmless."""
        # Always pass a context: imaplib's implicit one verifies nothing, which hands
        # the mailbox password to anyone able to intercept the connection.
        context = ssl.create_default_context()
        if tls == "insecure":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with mailbox_cls(host, port, timeout=_SOCKET_TIMEOUT_S, ssl_context=context).login(
            user, password, initial_folder=mailbox
        ) as mb:
            return list(mb.fetch(reverse=True, limit=limit, mark_seen=False, bulk=True))

    @staticmethod
    def _message_to_entry(msg: Any, feed_url: str) -> dict[str, Any]:
        mid = msg.headers.get("message-id")
        guid = (mid[0] if mid else "") or (msg.uid or "")
        if not guid:
            # No Message-ID and no server UID — hash stable fields so distinct
            # mails don't collapse to one guid (which dedupe would drop).
            basis = f"{msg.subject}{msg.from_}{getattr(msg, 'date_str', '')}"
            guid = hashlib.sha256(basis.encode("utf-8")).hexdigest()

        published: datetime | None = msg.date
        if published is not None and published.replace(tzinfo=None) == _NO_DATE:
            # Must land as NULL, not as 1900: only NULL is exempt from
            # max_entry_publish_age_days, so the sentinel silently drops the mail.
            published = None
        if published is not None:
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            published = published.astimezone(UTC)

        return {
            "guid": str(guid),
            "title": msg.subject or "(no subject)",
            "link": feed_url,
            "summary": msg.text or "",
            "content": msg.html or None,
            "author": msg.from_ or None,
            "published_at": published,
            "image_url": None,
        }


register_source_fetcher("email_imap", EmailSourceFetcher())
