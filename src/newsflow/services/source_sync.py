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
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.core.source_fetcher import declarable_source_types
from newsflow.models.base import get_session_factory
from newsflow.models.feed import Feed
from newsflow.models.subscription import Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService, SourceFeedConflictError

logger = logging.getLogger(__name__)

# Subscription.platform_user_id marker identifying rows this sync owns.
_OWNER = "source-yaml"
_SUB_PLATFORMS = frozenset({"discord", "telegram", "webhook"})


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


# ─── parsing ─────────────────────────────────────────────────────────────────


def parse_sources_yaml(path: Path) -> list[SourceCfg]:
    """Load and validate sources.yaml. Raises SourceConfigError on any
    structural problem so the operator sees it at boot."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SourceConfigError(f"couldn't read {path}: {e}") from e
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise SourceConfigError(f"malformed YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise SourceConfigError(f"{path}: top-level must be a mapping with a `sources:` key")

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

        url = cfg.get("url")
        if not url or not isinstance(url, str):
            raise SourceConfigError(f"source {name!r}: missing or non-string `url`")
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

        subscribers = _parse_subscribers(name, cfg.get("subscribers") or [])
        out.append(
            SourceCfg(name=name, url=url, type=stype, config=sconfig, subscribers=subscribers)
        )
    return out


def _parse_subscribers(source_name: str, raw: Any) -> list[SubscriberCfg]:
    if not isinstance(raw, list):
        raise SourceConfigError(f"source {source_name!r}: `subscribers` must be a list")
    out: list[SubscriberCfg] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SourceConfigError(f"source {source_name!r}: each subscriber must be a mapping")
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
        out.append(
            SubscriberCfg(
                platform=platform,
                channel=channel,
                translate=bool(item.get("translate", False)),
                language=str(item.get("language", "zh-CN")),
                silent=bool(item.get("silent", False)),
            )
        )
    return out


# ─── sync ────────────────────────────────────────────────────────────────────


async def sync_sources(path: Path) -> None:
    """Entry point: parse the file and reconcile non-RSS feeds + their
    subscriptions. Idempotent."""
    sources = parse_sources_yaml(path)
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
    sub_repo = SubscriptionRepository(session)

    desired_urls: set[str] = set()
    desired_subs: set[tuple[str, str, int]] = set()  # (platform, channel, feed_id)

    for src in sources:
        try:
            feed = await feed_service.upsert_source_feed(src.url, src.type, src.config)
        except SourceFeedConflictError as e:
            # The URL collides with a user's interactively-added RSS feed.
            # Skip this source (and its subscribers) rather than hijack the
            # feed; the RSS feed stays untouched and _remove_stale (non-RSS
            # only) never deletes it.
            logger.warning(f"source_sync: skipping source {src.name!r}: {e}")
            continue
        await session.flush()  # ensure feed.id is populated
        desired_urls.add(src.url)

        for sub_cfg in src.subscribers:
            desired_subs.add((sub_cfg.platform, sub_cfg.channel, feed.id))
            existing = await sub_repo.get_subscription(
                platform=sub_cfg.platform,
                channel_id=sub_cfg.channel,
                feed_id=feed.id,
            )
            if existing is None:
                sub = Subscription(
                    platform=sub_cfg.platform,
                    # No human user owns these — a literal marker tells future
                    # readers (and the removal logic) "owned by sources.yaml".
                    platform_user_id=_OWNER,
                    platform_channel_id=sub_cfg.channel,
                    feed_id=feed.id,
                    is_active=True,
                    silent=sub_cfg.silent,
                    translate=sub_cfg.translate,
                    target_language=sub_cfg.language,
                )
                session.add(sub)
                await session.flush()
                # Don't flood the channel with the source's whole backlog on
                # first sync — same policy as webhook_sync / regular /feed add.
                await sub_repo.seed_sent_entries(sub.id, feed.id, keep_latest=0)
                logger.info(
                    f"source_sync: subscribed {sub_cfg.platform}/{sub_cfg.channel} "
                    f"→ {src.name!r}"
                )
            elif existing.platform_user_id != _OWNER:
                # Defense in depth: a sub at this (platform, channel, feed)
                # that we don't own must never be silently rewritten by the
                # file. (Can't normally happen — interactive subs are on RSS
                # feeds — but guard anyway.)
                logger.warning(
                    f"source_sync: subscription {existing.platform}/"
                    f"{existing.platform_channel_id} → feed_id={feed.id} is "
                    f"owned by {existing.platform_user_id!r}, not sources.yaml;"
                    f" leaving its settings untouched"
                )
            else:
                # Keep settings in sync with the file so operators can change
                # them by editing and restarting.
                existing.silent = sub_cfg.silent
                existing.translate = sub_cfg.translate
                existing.target_language = sub_cfg.language
                if not existing.is_active:
                    existing.is_active = True

    await _remove_stale(session, desired_urls, desired_subs)


async def _remove_stale(
    session: AsyncSession,
    desired_urls: set[str],
    desired_subs: set[tuple[str, str, int]],
) -> None:
    known = declarable_source_types()

    # 1. Drop non-RSS feeds that left the file. Deleting a feed cascades to
    #    ALL of its subscriptions and their SentEntry dedupe history —
    #    including rows this sync does not own (an interactive /feed add on
    #    the same URL, or webhooks.yaml's "yaml" rows). A feed with foreign
    #    subscribers is therefore kept alive (it keeps fetching for them);
    #    step 2 below still removes the source-yaml subscriptions, which is
    #    all we own.
    feeds_result = await session.execute(select(Feed).where(Feed.source_type.in_(known)))
    for feed in feeds_result.scalars().all():
        if feed.url in desired_urls:
            continue
        # Explicit COUNT rather than feed.subscriptions: the selectin
        # collection is not refreshed for a feed already in this session's
        # identity map, so it can miss subscriptions created after the feed
        # was first loaded.
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

    # 2. Drop our subscriptions whose (platform, channel, feed) left the file
    #    while the source itself stayed (a subscriber was removed).
    subs_result = await session.execute(
        select(Subscription).where(Subscription.platform_user_id == _OWNER)
    )
    for sub in subs_result.scalars().all():
        if (sub.platform, sub.platform_channel_id, sub.feed_id) not in desired_subs:
            logger.info(
                f"source_sync: unsubscribing {sub.platform}/{sub.platform_channel_id} "
                f"→ feed_id={sub.feed_id}"
            )
            await session.delete(sub)
    await session.flush()
