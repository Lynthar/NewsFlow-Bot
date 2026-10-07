"""WebhookAdapter — push feed entries to arbitrary HTTP endpoints.

Unlike Discord/Telegram, this adapter has no bot UI; it's a send-only
platform. Subscriptions exist as normal `Subscription` rows with
`platform="webhook"` and `platform_channel_id=<destination name>`. The
mapping from destination name → URL/format/secret lives in the
`webhook_destinations` table, populated declaratively by `webhooks.yaml`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Mapping
from enum import Enum, auto
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy import select

from newsflow.adapters.base import BaseAdapter, Message, UndeliverableError, bare_message
from newsflow.adapters.webhook.formats import (
    Refusal,
    WireRequest,
    body_refusal,
    build_notification_payload,
    build_payload,
    reads_verdict_from_body,
)
from newsflow.core.env_refs import expand_env_refs
from newsflow.core.feed_fetcher import read_body_capped
from newsflow.models.base import get_session_factory
from newsflow.models.webhook import WebhookDestination
from newsflow.services.dispatcher import get_dispatcher

logger = logging.getLogger(__name__)


def _retry_after_seconds(headers: Mapping[str, str], cap: float) -> float | None:
    """How long to wait before retrying a rate-limited send.

    Returns:
        Seconds to wait; 1.0 when no usable header is present (`Retry-After`
        may legally be an HTTP-date); None when the wait exceeds `cap` and the
        caller should defer to the next dispatch round rather than stall.
    """
    # X-RateLimit-Reset-After first: Discord answers webhook 429s with a
    # Retry-After in milliseconds, which read as seconds defers every entry.
    raw = headers.get("X-RateLimit-Reset-After") or headers.get("Retry-After")
    if raw is None:
        return 1.0
    try:
        delay = float(raw)
    except ValueError:
        return 1.0
    return None if delay > cap else max(0.0, delay)


def _loggable_host(url: str) -> str:
    """`hostname[:port]` of `url`, without the userinfo that `netloc` would carry."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or "<no-host>"
    return f"{host}:{port}" if port else host


# A verdict body is a few dozen bytes; anything past this is not one.
_VERDICT_BODY_CAP = 4096

# Statuses that judge the payload rather than the endpoint: they never charge the breaker,
# or one feed's unpostable entries would disable a destination every other feed shares.
_CONTENT_REJECTIONS = frozenset({400, 413, 422})


class _Outcome(Enum):
    SENT = auto()
    # A _CONTENT_REJECTIONS status. The breaker is not charged.
    REJECTED = auto()
    # A 2xx whose body reports a refusal. Not charged yet: the caller decides.
    REFUSED = auto()
    # Already charged to the breaker, or deferred by a rate limit.
    FAILED = auto()


async def _read_refusal(format_name: str, resp: aiohttp.ClientResponse) -> Refusal | None:
    if not reads_verdict_from_body(format_name):
        return None
    body = await read_body_capped(resp.content, _VERDICT_BODY_CAP)
    return None if body is None else body_refusal(format_name, body)


class WebhookAdapter(BaseAdapter):
    """Send feed messages / system notices to configured HTTP endpoints."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._destinations: dict[str, WebhookDestination] = {}
        self._started = False
        # start() blocks on this until stop() is called; without it start returns
        # immediately and main.py's gather never runs the aiohttp session cleanup.
        self._stop_event: asyncio.Event | None = None

    @property
    def platform_name(self) -> str:
        return "webhook"

    def is_connected(self) -> bool:
        """Webhook has no persistent connection; 'connected' just means the
        aiohttp session is live and we've loaded destinations at least once."""
        return self._started and self._session is not None and not self._session.closed

    async def reload_destinations(self) -> None:
        """Refresh the in-memory destination cache from DB. Called at startup
        after webhook_sync has run; also safe to call at runtime if someone
        wires a reload signal later."""
        session_factory = get_session_factory()
        async with session_factory() as session:
            result = await session.execute(select(WebhookDestination))
            self._destinations = {d.name: d for d in result.scalars().all()}
        logger.info(
            f"WebhookAdapter loaded {len(self._destinations)} destination(s): "
            f"{sorted(self._destinations)}"
        )

    async def start(self) -> None:
        """Open the aiohttp session, register with the dispatcher, and block
        until stop() is called (so the task stays alive for cleanup)."""
        self._session = aiohttp.ClientSession()
        self._stop_event = asyncio.Event()
        # Everything after the session is opened sits in the try: a shutdown can
        # cancel this task while it is still loading destinations.
        try:
            await self.reload_destinations()
            self._started = True
            get_dispatcher().register_adapter("webhook", self)
            logger.info("WebhookAdapter registered with dispatcher")
            await self._stop_event.wait()
        finally:
            self._started = False
            if self._session is not None and not self._session.closed:
                await self._session.close()
            logger.info("WebhookAdapter stopped")

    async def stop(self) -> None:
        """Signal start() to unblock so it can run its cleanup finally."""
        if self._stop_event is not None:
            self._stop_event.set()

    async def send_message(self, channel_id: str, message: Message) -> bool:
        """Post an entry. Raises UndeliverableError when the receiver answers a
        _CONTENT_REJECTIONS status to the entry and to its bare title-and-link form."""
        dest = self._destinations.get(channel_id)
        if dest is None:
            logger.warning(f"webhook send: destination {channel_id!r} not configured")
            return False
        if dest.is_active is False:
            return False  # breaker open — backlog retries cheaply, no network
        outcome, error = await self._post(dest, build_payload(dest.format, message))
        if outcome in (_Outcome.REJECTED, _Outcome.REFUSED):
            bare = build_payload(dest.format, bare_message(message))
            outcome, error = await self._post(dest, bare)
        if outcome is _Outcome.REJECTED:
            raise UndeliverableError(dest.name, reason=error or "rejected")
        if outcome is _Outcome.REFUSED:
            await self._record_send_result(dest, ok=False, error=error)
        return outcome is _Outcome.SENT

    async def send_text(self, channel_id: str, text: str) -> bool:
        dest = self._destinations.get(channel_id)
        if dest is None or dest.is_active is False:
            return False
        outcome, error = await self._post(dest, build_notification_payload(dest.format, text))
        if outcome is _Outcome.REFUSED:
            await self._record_send_result(dest, ok=False, error=error)
        return outcome is _Outcome.SENT

    async def _post(
        self, dest: WebhookDestination, wire: WireRequest
    ) -> tuple[_Outcome, str | None]:
        """POST the wire body to dest.url with format-default headers, any
        user-supplied headers, and an HMAC signature if dest.secret is set.

        Returns:
            The outcome, and for REJECTED / REFUSED the error to report.
        """
        headers: dict[str, str] = dict(wire.headers)
        try:
            # ${VAR} references are stored unexpanded: the secrets never reach the database.
            url = expand_env_refs(dest.url, f"webhook {dest.name!r}: url")
            secret = (
                expand_env_refs(dest.secret, f"webhook {dest.name!r}: secret")
                if dest.secret
                else None
            )
            if dest.headers:
                # Cast to str — SQLAlchemy JSON returns whatever the user wrote,
                # which could be numbers or bools if they were careless.
                headers.update(
                    {
                        k: expand_env_refs(str(v), f"webhook {dest.name!r}: headers[{k!r}]")
                        for k, v in dest.headers.items()
                    }
                )
        except ValueError as e:
            logger.warning(str(e))
            await self._record_send_result(dest, ok=False, error=str(e))
            return _Outcome.FAILED, None
        if secret:
            # Sign the exact bytes we're about to send. Receiver computes the
            # same HMAC and compares. Prevents tampering on open endpoints.
            sig = hmac.new(secret.encode("utf-8"), wire.body, hashlib.sha256).hexdigest()
            headers["X-NewsFlow-Signature"] = f"sha256={sig}"

        # Log host only — the full URL often contains a secret token (Slack,
        # Zapier, feishu signed URLs all do) that shouldn't land in logs.
        host = _loggable_host(url)
        timeout = aiohttp.ClientTimeout(total=max(1, dest.timeout_s))

        for attempt in range(2):
            # Re-checked per attempt: shutdown can close the session while the
            # retry below is sleeping.
            if self._session is None or self._session.closed:
                logger.error("webhook send attempted with no open aiohttp session")
                return _Outcome.FAILED, None
            try:
                # allow_redirects=False: a webhook answering a POST with a redirect is a
                # misconfiguration, and following it would re-send the signed body and auth
                # headers to a URL the operator never vetted.
                async with self._session.post(
                    url,
                    data=wire.body,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                ) as resp:
                    refusal: Refusal | None = None
                    if 200 <= resp.status < 300:
                        refusal = await _read_refusal(dest.format, resp)
                        if refusal is None:
                            await self._record_send_result(dest, ok=True)
                            return _Outcome.SENT, None
                    if refusal is not None and refusal.rate_limited:
                        # No wait to honour: the code says only "slow down", and WeCom's
                        # window is a minute. Like a 429, it never touches the breaker.
                        logger.warning(
                            f"webhook {dest.name} ({host}) rate-limited ({refusal.error}); "
                            f"deferring to the next dispatch round"
                        )
                        return _Outcome.FAILED, None
                    if resp.status == 429:
                        # The receiver is pacing us, not failing. Neither branch touches the
                        # breaker: 10 rate-limits in a row would disable a healthy endpoint,
                        # and crediting a success would clear real failures.
                        delay = _retry_after_seconds(resp.headers, self._MAX_RETRY_AFTER_S)
                        if attempt == 0 and delay is not None:
                            logger.info(
                                f"webhook {dest.name} ({host}) rate-limited; retrying in {delay}s"
                            )
                            await asyncio.sleep(delay)
                            continue
                        logger.warning(
                            f"webhook {dest.name} ({host}) rate-limited; "
                            f"deferring to the next dispatch round"
                        )
                        return _Outcome.FAILED, None
                    if refusal is not None:
                        logger.warning(f"webhook {dest.name} ({host}) refused: {refusal.error}")
                        return _Outcome.REFUSED, refusal.error
                    # Read a small slice of the body for diagnostics without
                    # letting a misbehaving server push megabytes into our logs.
                    snippet = (await resp.content.read(512)).decode("utf-8", errors="replace")
                    logger.warning(f"webhook {dest.name} ({host}) HTTP {resp.status}: {snippet!r}")
                    if resp.status in _CONTENT_REJECTIONS:
                        return _Outcome.REJECTED, f"HTTP {resp.status}"
                    await self._record_send_result(dest, ok=False, error=f"HTTP {resp.status}")
                    return _Outcome.FAILED, None
            except TimeoutError:
                logger.warning(f"webhook {dest.name} ({host}) timed out after {dest.timeout_s}s")
                await self._record_send_result(
                    dest, ok=False, error=f"timeout after {dest.timeout_s}s"
                )
                return _Outcome.FAILED, None
            except aiohttp.ClientError as e:
                # Type name only: aiohttp's message can quote the whole URL, token included.
                error = type(e).__name__
                logger.warning(f"webhook {dest.name} ({host}) client error: {error}")
                await self._record_send_result(dest, ok=False, error=error)
                return _Outcome.FAILED, None
            except ValueError as e:
                # aiohttp raises ValueError on an illegal header value. Treat it as a failed send
                # rather than letting it escape and wedge the entry in the dispatch loop.
                error = type(e).__name__
                logger.warning(f"webhook {dest.name} ({host}) bad header/request: {error}")
                await self._record_send_result(dest, ok=False, error=error)
                return _Outcome.FAILED, None
        return _Outcome.FAILED, None

    # Consecutive-failure threshold; mirrors Feed.mark_error's hardcoded 10.
    _MAX_CONSECUTIVE_ERRORS = 10

    # Ceiling on a 429 wait. Dispatch is serial, so a long sleep here delays every
    # other destination and platform — same reason timeout_s is capped.
    _MAX_RETRY_AFTER_S = 5.0

    async def _record_send_result(
        self, dest: WebhookDestination, *, ok: bool, error: str | None = None
    ) -> None:
        """Track consecutive failures on the destination (cache + DB) and trip
        the breaker at the threshold. Accounting must never break a send, so
        every DB problem here is swallowed with a log line.

        Success only writes when it RESETS a non-zero counter — the happy
        path stays free of per-send DB writes.
        """
        if dest.id is None:
            return  # transient instance (never persisted) — nothing to track
        if ok and not dest.error_count:
            return
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                row = await session.get(WebhookDestination, dest.id)
                if row is None:
                    return
                if ok:
                    row.is_active = True
                    row.error_count = 0
                    row.last_error = None
                else:
                    row.error_count += 1
                    row.last_error = (error or "send failed")[:512]
                    if row.error_count >= self._MAX_CONSECUTIVE_ERRORS and row.is_active:
                        row.is_active = False
                        logger.error(
                            f"webhook destination {dest.name!r} auto-disabled after "
                            f"{row.error_count} straight failures (last: {row.last_error}). "
                            "Fix the endpoint, then hot-reload (SIGHUP or "
                            "POST /api/admin/reload) or restart to re-enable; "
                            "undelivered entries within retention will then flush."
                        )
                await session.commit()
                # Mirror onto the cached instance the send path consults.
                dest.is_active = row.is_active
                dest.error_count = row.error_count
                dest.last_error = row.last_error
        except Exception:
            logger.exception(f"failed to record webhook send result for {dest.name!r}")


# Module-level singleton — mirrors the start_discord / start_telegram pattern
# so main.py can uniformly do `tasks.append(start_webhook())`.
_adapter: WebhookAdapter | None = None


async def start_webhook() -> None:
    """Entry point for main.py to spawn as an asyncio task."""
    global _adapter
    _adapter = WebhookAdapter()
    await _adapter.start()


async def stop_webhook() -> None:
    """Signal the start task to exit its wait-loop and clean up."""
    global _adapter
    if _adapter is not None:
        await _adapter.stop()
        _adapter = None
