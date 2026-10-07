"""Reconcile non-RSS source feeds + their subscriptions from a YAML file.

``sources.yaml`` declares feeds that aren't plain RSS (JSON-API, IMAP email, …)
together with the channels that should receive their entries. It's the
declarative counterpart to interactive ``/feed add`` (which only handles RSS)
and mirrors ``webhooks.yaml``: the file is the source of truth, reconciled on
every startup. The schema is documented in ``samples/sources.example.yaml``.

Only these rows are ever modified or removed here, so RSS feeds and
interactively-created subscriptions are never touched:
- Feeds with a non-RSS ``source_type``.
- Subscriptions with ``platform_user_id == "source-yaml"``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.core.source_fetcher import declarable_source_types
from newsflow.models.base import get_session_factory
from newsflow.models.feed import Feed
from newsflow.models.subscription import SubscriberPlatform, Subscription
from newsflow.services._owned_subscriptions import (
    SOURCES_OWNER,
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
from newsflow.services.feed_service import FeedService, SourceFeedConflictError

logger = logging.getLogger(__name__)

_OWNER = SOURCES_OWNER
_SUB_PLATFORMS: frozenset[str] = frozenset(get_args(SubscriberPlatform))


class SourceConfigError(ValueError):
    """Raised when sources.yaml is malformed. Startup fails fast on this rather
    than limping with a half-synced state."""


@dataclass
class SubscriberCfg:
    platform: str
    channel: str
    translate: bool = False
    language: str = "zh-CN"
    silent: bool = False


@dataclass
class SourceCfg:
    name: str
    url: str
    type: str
    config: dict[str, Any]
    subscribers: list[SubscriberCfg] = field(default_factory=list)
    # Per-source fetch cadence, stored in Feed.config under a reserved key. The global
    # loop still ticks every FETCH_INTERVAL_MINUTES; a longer per-source interval
    # just skips the earlier ticks.
    fetch_interval_minutes: int | None = None


# Unknown keys are rejected, not ignored — a typo'd key used to vanish
# silently. `config:` stays free-form on purpose: its keys belong to the
# individual SourceFetcher contracts, not this schema.
_TOP_LEVEL_KEYS = frozenset({"sources"})
_SOURCE_KEYS = yaml_keys(SourceCfg) - {"name"}  # `name` is the mapping key, not a block key
_SUBSCRIBER_KEYS = yaml_keys(SubscriberCfg)


# ─── parsing ─────────────────────────────────────────────────────────────────


def parse_sources_yaml(path: Path) -> list[SourceCfg]:
    """Load and validate sources.yaml. Raises SourceConfigError on any
    structural problem so the operator sees it at boot."""
    raw = load_yaml(SourceConfigError, path)
    if not isinstance(raw, dict):
        raise SourceConfigError(f"{path}: top-level must be a mapping with a `sources:` key")
    reject_unknown_keys(SourceConfigError, f"{path}", raw, _TOP_LEVEL_KEYS)

    sources_raw = raw.get("sources") or {}
    if not isinstance(sources_raw, dict):
        raise SourceConfigError("`sources` must be a mapping of name -> {url, type, ...}")

    known = declarable_source_types()
    out: list[SourceCfg] = []
    seen_urls: set[str] = set()
    for name, cfg in sources_raw.items():
        if not isinstance(name, str) or not name:
            raise SourceConfigError(f"source name must be a non-empty string, got {name!r}")
        if not isinstance(cfg, dict):
            raise SourceConfigError(f"source {name!r}: must be a mapping")
        reject_unknown_keys(SourceConfigError, f"source {name!r}", cfg, _SOURCE_KEYS)

        url = cfg.get("url")
        if not url or not isinstance(url, str):
            raise SourceConfigError(f"source {name!r}: missing or non-string `url`")
        require_fits(SourceConfigError, f"source {name!r}", "url", url, Feed.url)
        if url in seen_urls:
            raise SourceConfigError(f"source {name!r}: duplicate url {url!r}")
        seen_urls.add(url)

        stype = cfg.get("type")
        if stype not in known:
            raise SourceConfigError(
                f"source {name!r}: unknown type {stype!r}. Known: {sorted(known)}"
            )

        sconfig = cfg.get("config") or {}
        if not isinstance(sconfig, dict):
            raise SourceConfigError(f"source {name!r}: `config` must be a mapping")
        if "fetch_interval_minutes" in sconfig:
            raise SourceConfigError(
                f"source {name!r}: `fetch_interval_minutes` is a source-level "
                "key, not a config key — move it up one level"
            )

        interval = cfg.get("fetch_interval_minutes")
        if interval is not None and (not isinstance(interval, int) or interval < 1):
            raise SourceConfigError(
                f"source {name!r}: `fetch_interval_minutes` must be an integer >= 1"
            )

        subscribers = _parse_subscribers(name, cfg.get("subscribers") or [])
        out.append(
            SourceCfg(
                name=name,
                url=url,
                type=stype,
                config=sconfig,
                subscribers=subscribers,
                fetch_interval_minutes=interval,
            )
        )
    return out


def _parse_subscribers(source_name: str, raw: Any) -> list[SubscriberCfg]:
    if not isinstance(raw, list):
        raise SourceConfigError(f"source {source_name!r}: `subscribers` must be a list")
    out: list[SubscriberCfg] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SourceConfigError(f"source {source_name!r}: each subscriber must be a mapping")
        reject_unknown_keys(
            SourceConfigError, f"source {source_name!r} subscriber", item, _SUBSCRIBER_KEYS
        )
        platform = item.get("platform")
        if platform not in _SUB_PLATFORMS:
            raise SourceConfigError(
                f"source {source_name!r}: subscriber platform must be one of "
                f"{sorted(_SUB_PLATFORMS)}, got {platform!r}"
            )
        channel = item.get("channel")
        if not channel or not isinstance(channel, str):
            raise SourceConfigError(
                f"source {source_name!r}: subscriber needs a non-empty string `channel`"
            )
        ctx = f"source {source_name!r} subscriber"
        require_fits(SourceConfigError, ctx, "channel", channel, Subscription.platform_channel_id)
        out.append(
            SubscriberCfg(
                platform=platform,
                channel=channel,
                translate=require_bool(
                    SourceConfigError, ctx, "translate", item.get("translate"), False
                ),
                language=require_language(SourceConfigError, ctx, item.get("language"), "zh-CN"),
                silent=require_bool(SourceConfigError, ctx, "silent", item.get("silent"), False),
            )
        )
    return out


# ─── sync ────────────────────────────────────────────────────────────────────


def source_warnings(sources: list[SourceCfg]) -> list[str]:
    """Declarations that load but likely misbehave. Startup and reload log these;
    checkconfig reports them. Neither refuses to run over one."""
    return [
        f"source {src.name!r}: json_api without `guid` keys each item by a hash of all its "
        "fields, so an item whose counter or timestamp changes is delivered again; map "
        "`guid` to a stable id"
        for src in sources
        if src.type == "json_api" and not src.config.get("guid")
    ]


def undeclared_webhook_destinations(sources: list[SourceCfg], webhooks_path: Path) -> list[str]:
    """One message per webhook subscriber naming a destination webhooks.yaml does not
    declare: it would sync but never deliver. An unparsable webhooks.yaml is reported as
    that file's own error, so nothing is checked against it."""
    from newsflow.services.webhook_sync import WebhookConfigError, parse_webhooks_yaml

    refs = sorted(
        {
            (src.name, sub.channel)
            for src in sources
            for sub in src.subscribers
            if sub.platform == "webhook"
        }
    )
    if not refs:
        return []
    declared: set[str] = set()
    if webhooks_path.is_file():
        try:
            declared = set(parse_webhooks_yaml(webhooks_path).destinations)
        except WebhookConfigError:
            return []
    return [
        f"source {name!r} subscribes webhook destination {dest!r}, which webhooks.yaml "
        "does not declare — it would sync but never deliver"
        for name, dest in refs
        if dest not in declared
    ]


async def sync_sources(path: Path, webhooks_path: Path) -> None:
    """Entry point: parse the file and reconcile non-RSS feeds + their
    subscriptions. Idempotent.

    Raises:
        SourceConfigError: the file is invalid, or a webhook subscriber names a
            destination webhooks.yaml does not declare. Nothing is written then.
    """
    sources = parse_sources_yaml(path)
    undeclared = undeclared_webhook_destinations(sources, webhooks_path)
    if undeclared:
        raise SourceConfigError("; ".join(undeclared))
    for warning in source_warnings(sources):
        logger.warning(f"source_sync: {warning}")
    logger.info(
        f"source_sync: {len(sources)} source(s), "
        f"{sum(len(s.subscribers) for s in sources)} subscription(s) in {path}"
    )
    session_factory = get_session_factory()
    async with session_factory() as session:
        await _reconcile(session, sources)
        await session.commit()


async def _reconcile(session: AsyncSession, sources: list[SourceCfg]) -> None:
    feed_service = FeedService(session)

    desired_urls: set[str] = set()
    declared: list[DeclaredSubscription] = []

    for src in sources:
        # The reserved scheduling key rides inside Feed.config so no schema
        # change is needed; rebuilt every sync, so removing the key from the
        # file also removes it from the stored config.
        stored_config = dict(src.config)
        if src.fetch_interval_minutes is not None:
            stored_config["fetch_interval_minutes"] = src.fetch_interval_minutes
        try:
            feed = await feed_service.upsert_source_feed(src.url, src.type, stored_config)
        except SourceFeedConflictError as e:
            # URL collides with a user's interactively-added RSS feed. Skip this source
            # rather than hijack the feed; _remove_stale is non-RSS only, so it survives.
            logger.warning(f"source_sync: skipping source {src.name!r}: {e}")
            continue
        await session.flush()  # ensure feed.id is populated
        desired_urls.add(src.url)
        declared.extend(
            DeclaredSubscription(
                platform=sub_cfg.platform,
                channel_id=sub_cfg.channel,
                feed_id=feed.id,
                settings={
                    "silent": sub_cfg.silent,
                    "translate": sub_cfg.translate,
                    "target_language": sub_cfg.language,
                },
            )
            for sub_cfg in src.subscribers
        )

    await reconcile_owned_subscriptions(session, _OWNER, declared, "source_sync")
    await _remove_stale(session, desired_urls)


async def _remove_stale(session: AsyncSession, desired_urls: set[str]) -> None:
    """Drop non-RSS feeds that left the file. Deleting a feed cascades to ALL its
    subscriptions and their SentEntry history, including rows this sync does not own,
    so a feed with foreign subscribers is kept alive."""
    known = declarable_source_types()
    feeds_result = await session.execute(select(Feed).where(Feed.source_type.in_(known)))
    for feed in feeds_result.scalars().all():
        if feed.url in desired_urls:
            continue
        # Explicit COUNT rather than feed.subscriptions: the selectin collection is not
        # refreshed for a feed already in this session's identity map.
        foreign_count = (
            await session.execute(
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.feed_id == feed.id,
                    Subscription.platform_user_id != _OWNER,
                )
            )
        ).scalar_one()
        if foreign_count:
            logger.warning(
                f"source_sync: {feed.url!r} left sources.yaml but has "
                f"{foreign_count} subscription(s) owned elsewhere; keeping "
                f"the feed, removing only source-yaml subscriptions"
            )
            continue
        logger.info(f"source_sync: removing source feed {feed.url!r}")
        await session.delete(feed)
    await session.flush()
