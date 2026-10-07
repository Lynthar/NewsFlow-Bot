"""Reconcile the webhook_destinations + subscriptions tables with a YAML file.

Design: the YAML file is the single source of truth at boot. Every startup:
1. parse the file,
2. upsert destinations (new / changed URLs, formats, secrets),
3. remove destinations that disappeared from the file,
4. ensure each YAML subscription has a matching Subscription row,
5. remove webhook-platform subscriptions that dropped out of the file.

Feeds referenced by the YAML get auto-added if missing (same code path as
`/feed add`). This costs one network round-trip per new feed at startup;
existing feeds are cheap. If add_feed fails (404, parse error), we log a
warning and continue — the bot still starts.

The YAML structure is documented in `samples/webhooks.example.yaml`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.adapters.webhook.formats import SUPPORTED_FORMATS
from newsflow.core.env_refs import expand_env_refs
from newsflow.core.source_shortcuts import expand_source_shortcut
from newsflow.core.url_security import InvalidFeedURLError, validate_feed_url
from newsflow.models.base import get_session_factory
from newsflow.models.feed import Feed
from newsflow.models.webhook import WebhookDestination
from newsflow.services._owned_subscriptions import (
    WEBHOOKS_OWNER,
    DeclaredSubscription,
    reconcile_owned_subscriptions,
)
from newsflow.services._yamlcfg import (
    load_yaml,
    reject_unknown_keys,
    require_bool,
    require_fits,
    require_language,
    yaml_keys,
)
from newsflow.services.feed_service import FeedService

logger = logging.getLogger(__name__)


# Per-destination request timeout ceiling. Dispatch is serial, so a large
# timeout on one slow endpoint would stall delivery to every other platform.
# The webhook model docstring promises this stays "small"; enforce it here.
_MAX_WEBHOOK_TIMEOUT_S = 60

# sources.yaml also creates platform="webhook" rows (owner "source-yaml"), so every
# mutation below must filter on this marker or the two syncs delete each other on startup.
_OWNER = WEBHOOKS_OWNER


class WebhookConfigError(ValueError):
    """Raised when webhooks.yaml is malformed or semantically invalid.
    Startup fails fast on this rather than limping with a half-synced state."""


@dataclass
class WebhookConfigDestination:
    """Normalised view of one destination block in YAML."""

    name: str
    url: str
    format: str = "generic"
    secret: str | None = None
    headers: dict[str, Any] | None = None
    timeout_s: int = 10
    # Per-destination defaults inherited by every subscription pointing here.
    translate: bool = False
    language: str = "zh-CN"


@dataclass
class WebhookConfig:
    destinations: dict[str, WebhookConfigDestination] = field(default_factory=dict)
    subscriptions: dict[str, list[str]] = field(default_factory=dict)


# Unknown keys are rejected, not ignored: a typo'd `secert:` used to make the
# HMAC signature silently vanish. `python -m newsflow.checkconfig` validates
# the file offline before a deploy.
_TOP_LEVEL_KEYS = yaml_keys(WebhookConfig)
_DESTINATION_KEYS = yaml_keys(WebhookConfigDestination) - {"name"}  # `name` is the mapping key


# ─── parsing ─────────────────────────────────────────────────────────────────


def parse_webhooks_yaml(path: Path) -> WebhookConfig:
    """Load and validate webhooks.yaml. Raises WebhookConfigError on any
    structural problem so the operator sees it at boot, not hours later."""
    raw = load_yaml(WebhookConfigError, path)

    if not isinstance(raw, dict):
        raise WebhookConfigError(
            f"{path}: top-level must be a mapping with `destinations:` "
            f"and optional `subscriptions:` keys"
        )
    reject_unknown_keys(WebhookConfigError, f"{path}", raw, _TOP_LEVEL_KEYS)

    destinations = _parse_destinations(raw.get("destinations") or {})
    subscriptions = _parse_subscriptions(raw.get("subscriptions") or {}, destinations)
    return WebhookConfig(destinations=destinations, subscriptions=subscriptions)


def _parse_destinations(
    raw: Any,
) -> dict[str, WebhookConfigDestination]:
    if not isinstance(raw, dict):
        raise WebhookConfigError("`destinations` must be a mapping of name -> {url, format, ...}")

    out: dict[str, WebhookConfigDestination] = {}
    for name, cfg in raw.items():
        if not isinstance(name, str) or not name:
            raise WebhookConfigError(f"destination name must be a non-empty string, got {name!r}")
        if not isinstance(cfg, dict):
            raise WebhookConfigError(
                f"destination {name!r}: must be a mapping, got {type(cfg).__name__}"
            )
        context = f"destination {name!r}"
        reject_unknown_keys(WebhookConfigError, context, cfg, _DESTINATION_KEYS)
        require_fits(WebhookConfigError, context, "name", name, WebhookDestination.name)

        url = cfg.get("url")
        if not url or not isinstance(url, str):
            raise WebhookConfigError(f"destination {name!r}: missing or non-string `url`")
        require_fits(WebhookConfigError, context, "url", url, WebhookDestination.url)
        # The message leaves the URL out: these carry their token in the path or query.
        if urlsplit(_resolved(name, "url", url)).scheme not in ("http", "https"):
            raise WebhookConfigError(f"destination {name!r}: `url` must be http:// or https://")

        fmt = str(cfg.get("format", "generic"))
        if fmt not in SUPPORTED_FORMATS:
            raise WebhookConfigError(
                f"destination {name!r}: unsupported format {fmt!r}. "
                f"Supported: {sorted(SUPPORTED_FORMATS)}"
            )

        secret = cfg.get("secret")
        if secret is not None and not isinstance(secret, str):
            # int-coercion would lose leading zeros ("0123" → 123 → "123")
            # and silently produce a different HMAC key than intended.
            raise WebhookConfigError(f"destination {name!r}: `secret` must be a string (quote it)")
        if secret is not None:
            require_fits(WebhookConfigError, context, "secret", secret, WebhookDestination.secret)
            _resolved(name, "secret", secret)

        headers = cfg.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise WebhookConfigError(f"destination {name!r}: `headers` must be a mapping")
        if headers is not None:
            bad_keys = [k for k in headers if not isinstance(k, str)]
            if bad_keys:
                raise WebhookConfigError(
                    f"destination {name!r}: header names must be strings, got {bad_keys!r}"
                )
            for key, value in headers.items():
                _resolved(name, f"headers[{key!r}]", str(value))

        try:
            timeout_s = int(cfg.get("timeout_s", 10))
        except (TypeError, ValueError) as e:
            raise WebhookConfigError(f"destination {name!r}: `timeout_s` must be an integer") from e
        if timeout_s > _MAX_WEBHOOK_TIMEOUT_S:
            logger.warning(
                "destination %r: timeout_s=%d exceeds cap %ds; clamping",
                name,
                timeout_s,
                _MAX_WEBHOOK_TIMEOUT_S,
            )
            timeout_s = _MAX_WEBHOOK_TIMEOUT_S
        timeout_s = max(1, timeout_s)

        out[name] = WebhookConfigDestination(
            name=name,
            url=url,
            format=fmt,
            secret=secret,
            headers=headers,
            timeout_s=timeout_s,
            translate=require_bool(
                WebhookConfigError,
                f"destination {name!r}",
                "translate",
                cfg.get("translate"),
                False,
            ),
            language=require_language(WebhookConfigError, context, cfg.get("language"), "zh-CN"),
        )
    return out


def _resolved(destination: str, field: str, value: str) -> str:
    """`value` with its ${VAR} references expanded. They are stored unexpanded and resolved
    at send time; an unset one fails here, at startup or reload, not on every send."""
    try:
        return expand_env_refs(value, f"destination {destination!r}: `{field}`")
    except ValueError as e:
        raise WebhookConfigError(str(e)) from e


def _parse_subscriptions(
    raw: Any,
    known_destinations: dict[str, WebhookConfigDestination],
) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise WebhookConfigError(
            "`subscriptions` must be a mapping of destination -> [feed_url, ...]"
        )

    out: dict[str, list[str]] = {}
    for dest_name, feeds in raw.items():
        if dest_name not in known_destinations:
            raise WebhookConfigError(
                f"subscriptions reference unknown destination {dest_name!r}. "
                f"Known: {sorted(known_destinations)}"
            )
        if not isinstance(feeds, list):
            raise WebhookConfigError(f"subscriptions[{dest_name!r}] must be a list of feed URLs")
        # dedupe while preserving order — lets users write the same feed twice
        # without producing a duplicate row.
        seen: set[str] = set()
        deduped: list[str] = []
        for u in feeds:
            if not isinstance(u, str):
                raise WebhookConfigError(
                    f"subscriptions[{dest_name!r}]: feed URL must be a string, got {u!r}"
                )
            context = f"subscriptions[{dest_name!r}]"
            require_fits(
                WebhookConfigError, context, "feed URL", expand_source_shortcut(u), Feed.url
            )
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        out[dest_name] = deduped
    return out


# ─── sync ────────────────────────────────────────────────────────────────────


async def sync_webhooks(path: Path) -> None:
    """Entry point: parse the file and reconcile the DB.

    Idempotent — running it twice in a row is a no-op on the second call.
    """
    config = parse_webhooks_yaml(path)
    logger.info(
        f"webhook_sync: {len(config.destinations)} destination(s), "
        f"{sum(len(v) for v in config.subscriptions.values())} subscription(s) "
        f"in {path}"
    )

    session_factory = get_session_factory()
    async with session_factory() as session:
        await _sync_destinations(session, config)
        await session.commit()
        feed_ids = await _resolve_feeds(session, config)
        declared = [
            DeclaredSubscription(
                platform="webhook",
                channel_id=dest_name,
                feed_id=feed_ids[url],
                settings={
                    "translate": config.destinations[dest_name].translate,
                    "target_language": config.destinations[dest_name].language,
                },
            )
            for dest_name, urls in config.subscriptions.items()
            for url in urls
            if url in feed_ids
        ]
        await reconcile_owned_subscriptions(session, _OWNER, declared, "webhook_sync")
        await session.commit()


async def _sync_destinations(session: AsyncSession, config: WebhookConfig) -> None:
    result = await session.execute(select(WebhookDestination))
    existing = {d.name: d for d in result.scalars().all()}

    # Upsert every destination from YAML.
    for name, cfg in config.destinations.items():
        row = existing.get(name)
        if row is None:
            session.add(
                WebhookDestination(
                    name=cfg.name,
                    url=cfg.url,
                    format=cfg.format,
                    secret=cfg.secret,
                    headers=cfg.headers,
                    timeout_s=cfg.timeout_s,
                )
            )
            logger.info(f"webhook_sync: added destination {name!r}")
        else:
            row.url = cfg.url
            row.format = cfg.format
            row.secret = cfg.secret
            row.headers = cfg.headers
            row.timeout_s = cfg.timeout_s
            if not row.is_active or row.error_count:
                # Still declared in the file = the operator wants it working, same revival
                # contract as auto-disabled feeds. Sync runs at startup and on hot reload.
                row.is_active = True
                row.error_count = 0
                row.last_error = None
                logger.info(f"webhook_sync: re-enabled destination {name!r}")

    # Drop destinations that left the YAML. Our subscriptions to them are no longer
    # declared, so the reconcile removes them; sources.yaml's rows are its own.
    for name in set(existing) - set(config.destinations):
        await session.delete(existing[name])
        logger.info(f"webhook_sync: removed destination {name!r}")

    await session.flush()


async def _resolve_feeds(session: AsyncSession, config: WebhookConfig) -> dict[str, int]:
    """The feed id for every URL the file subscribes to, adding new feeds with a fetch.
    Each URL is committed before the next one is fetched: a write transaction held
    across the network blocks every other writer, a dispatch round's sent-marks first."""
    feed_service = FeedService(session)
    feed_ids: dict[str, int] = {}
    for url in dict.fromkeys(u for urls in config.subscriptions.values() for u in urls):
        feed = await feed_service.get_feed_by_url(url)
        if feed is None:
            feed = await _add_feed(feed_service, url)
        elif not feed.is_active:
            # Still declared in the file = the operator wants it working.
            # Revive an auto-disabled feed on restart (the deactivation
            # notice promises exactly this for YAML-declared feeds).
            feed.reactivate()
            logger.info(f"webhook_sync: reactivated auto-disabled feed {url!r}")
        if feed is not None:
            feed_ids[url] = feed.id
        await session.commit()
    return feed_ids


async def _add_feed(feed_service: FeedService, url: str) -> Feed | None:
    """A new feed for `url`, as `/feed add` adds one (shortcuts, page discovery). A
    first fetch that fails still adds it, unfetched: the dispatch loop retries it, and
    its first successful fetch seeds only what predates the subscription."""
    logger.info(f"webhook_sync: fetching new feed {url!r}")
    result = await feed_service.add_feed(url)
    if result.success and result.feed is not None:
        return result.feed
    target = expand_source_shortcut(url)
    try:
        validate_feed_url(target)
    except InvalidFeedURLError as e:
        logger.warning(f"webhook_sync: skipping {url!r} — {e}")
        return None
    logger.warning(
        f"webhook_sync: first fetch of {url!r} failed ({result.message}); "
        "subscribing anyway, the dispatch loop will retry it"
    )
    return await feed_service.repo.create_feed(url=target)
