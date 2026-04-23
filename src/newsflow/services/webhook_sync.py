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

import yaml
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.adapters.webhook.formats import SUPPORTED_FORMATS
from newsflow.models.base import get_session_factory
from newsflow.models.subscription import Subscription
from newsflow.models.webhook import WebhookDestination
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService

logger = logging.getLogger(__name__)


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
    translate: bool = True
    language: str = "zh-CN"


@dataclass
class WebhookConfig:
    destinations: dict[str, WebhookConfigDestination] = field(default_factory=dict)
    subscriptions: dict[str, list[str]] = field(default_factory=dict)


# ─── parsing ─────────────────────────────────────────────────────────────────


def parse_webhooks_yaml(path: Path) -> WebhookConfig:
    """Load and validate webhooks.yaml. Raises WebhookConfigError on any
    structural problem so the operator sees it at boot, not hours later."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise WebhookConfigError(f"couldn't read {path}: {e}") from e

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise WebhookConfigError(f"malformed YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise WebhookConfigError(
            f"{path}: top-level must be a mapping with `destinations:` "
            f"and optional `subscriptions:` keys"
        )

    destinations = _parse_destinations(raw.get("destinations") or {})
    subscriptions = _parse_subscriptions(raw.get("subscriptions") or {}, destinations)
    return WebhookConfig(destinations=destinations, subscriptions=subscriptions)


def _parse_destinations(
    raw: Any,
) -> dict[str, WebhookConfigDestination]:
    if not isinstance(raw, dict):
        raise WebhookConfigError(
            "`destinations` must be a mapping of name -> {url, format, ...}"
        )

    out: dict[str, WebhookConfigDestination] = {}
    for name, cfg in raw.items():
        if not isinstance(name, str) or not name:
            raise WebhookConfigError(f"destination name must be a non-empty string, got {name!r}")
        if not isinstance(cfg, dict):
            raise WebhookConfigError(
                f"destination {name!r}: must be a mapping, got {type(cfg).__name__}"
            )

        url = cfg.get("url")
        if not url or not isinstance(url, str):
            raise WebhookConfigError(f"destination {name!r}: missing or non-string `url`")

        fmt = str(cfg.get("format", "generic"))
        if fmt not in SUPPORTED_FORMATS:
            raise WebhookConfigError(
                f"destination {name!r}: unsupported format {fmt!r}. "
                f"Supported: {sorted(SUPPORTED_FORMATS)}"
            )

        headers = cfg.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise WebhookConfigError(f"destination {name!r}: `headers` must be a mapping")

        try:
            timeout_s = int(cfg.get("timeout_s", 10))
        except (TypeError, ValueError) as e:
            raise WebhookConfigError(
                f"destination {name!r}: `timeout_s` must be an integer"
            ) from e

        out[name] = WebhookConfigDestination(
            name=name,
            url=url,
            format=fmt,
            secret=cfg.get("secret"),
            headers=headers,
            timeout_s=timeout_s,
            translate=bool(cfg.get("translate", True)),
            language=str(cfg.get("language", "zh-CN")),
        )
    return out


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
            raise WebhookConfigError(
                f"subscriptions[{dest_name!r}] must be a list of feed URLs"
            )
        # dedupe while preserving order — lets users write the same feed twice
        # without producing a duplicate row.
        seen: set[str] = set()
        deduped: list[str] = []
        for u in feeds:
            if not isinstance(u, str):
                raise WebhookConfigError(
                    f"subscriptions[{dest_name!r}]: feed URL must be a string, got {u!r}"
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
        await _sync_subscriptions(session, config)
        await session.commit()


async def _sync_destinations(
    session: AsyncSession, config: WebhookConfig
) -> None:
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

    # Drop destinations that disappeared from YAML, and their subscriptions.
    # Subscriptions reference the destination via string name (not FK) so we
    # have to delete them explicitly.
    for name in set(existing) - set(config.destinations):
        await session.execute(
            delete(Subscription).where(
                Subscription.platform == "webhook",
                Subscription.platform_channel_id == name,
            )
        )
        await session.delete(existing[name])
        logger.info(f"webhook_sync: removed destination {name!r}")

    await session.flush()


async def _sync_subscriptions(
    session: AsyncSession, config: WebhookConfig
) -> None:
    feed_service = FeedService(session)
    sub_repo = SubscriptionRepository(session)

    desired: set[tuple[str, int]] = set()  # (destination_name, feed_id)

    for dest_name, feed_urls in config.subscriptions.items():
        dest_cfg = config.destinations[dest_name]
        for url in feed_urls:
            feed = await feed_service.get_feed_by_url(url)
            if feed is None:
                # New feed — add via the usual path so it gets fetched, parsed,
                # and seeded with initial entries like any other feed.
                logger.info(
                    f"webhook_sync: fetching new feed {url!r} for {dest_name!r}"
                )
                add_result = await feed_service.add_feed(url)
                if not add_result.success or add_result.feed is None:
                    logger.warning(
                        f"webhook_sync: skipping {url!r} — {add_result.message}"
                    )
                    continue
                feed = add_result.feed

            desired.add((dest_name, feed.id))

            existing = await sub_repo.get_subscription(
                platform="webhook",
                channel_id=dest_name,
                feed_id=feed.id,
            )
            if existing is None:
                sub = Subscription(
                    platform="webhook",
                    # platform_user_id is NOT NULL but webhook has no human
                    # user; "yaml" is a stable literal telling future readers
                    # "owned by the YAML config".
                    platform_user_id="yaml",
                    platform_channel_id=dest_name,
                    feed_id=feed.id,
                    is_active=True,
                    translate=dest_cfg.translate,
                    target_language=dest_cfg.language,
                )
                session.add(sub)
                await session.flush()
                # Don't flood the webhook with the feed's entire backlog on
                # first sync. Let the next dispatch cycle deliver from zero
                # new entries onward (same policy as regular /feed add).
                await sub_repo.seed_sent_entries(
                    sub.id, feed.id, keep_latest=0
                )
                logger.info(
                    f"webhook_sync: subscribed {dest_name!r} → {url!r}"
                )
            else:
                # Keep translate / language in sync with the YAML defaults so
                # operators can flip them by editing the file and restarting.
                existing.translate = dest_cfg.translate
                existing.target_language = dest_cfg.language
                if not existing.is_active:
                    existing.is_active = True

    # Drop webhook subscriptions that dropped out of the YAML.
    result = await session.execute(
        select(Subscription).where(Subscription.platform == "webhook")
    )
    for sub in result.scalars().all():
        if (sub.platform_channel_id, sub.feed_id) not in desired:
            logger.info(
                f"webhook_sync: unsubscribing {sub.platform_channel_id!r} → "
                f"feed_id={sub.feed_id}"
            )
            await session.delete(sub)

    await session.flush()
