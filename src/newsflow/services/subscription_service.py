"""
Subscription service - Business logic for subscription management.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.config import get_settings
from newsflow.core.filter import FilterRule
from newsflow.core.opml import OpmlEntry, OpmlParseError, build_opml, parse_opml
from newsflow.core.source_shortcuts import expand_source_shortcut
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.channel_settings_repository import ChannelSettingsRepository
from newsflow.repositories.feed_repository import FeedRepository
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService

logger = logging.getLogger(__name__)


@dataclass
class SubscribeResult:
    """Result of subscribing to a feed."""

    success: bool
    subscription: Subscription | None = None
    feed: Feed | None = None
    message: str = ""
    is_new: bool = False


@dataclass
class UnsubscribeResult:
    """Result of unsubscribing from a feed."""

    success: bool
    message: str = ""


@dataclass
class SubscriptionActionResult:
    """Result of a simple state change on a subscription (pause/resume)."""

    success: bool
    message: str = ""


@dataclass
class SubscriptionDetail:
    """Data transfer object for /feed status. Composes a subscription with
    its owning feed and a few recent entries for context."""

    subscription: Subscription
    feed: Feed
    recent_entries: list[FeedEntry]


@dataclass
class OpmlImportResult:
    """Outcome of bulk-subscribing from an OPML file."""

    added: list[str]  # URLs newly subscribed in this call
    already_subscribed: list[str]  # URLs already subscribed in the channel
    failed: list[tuple[str, str]]  # (url, reason)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.already_subscribed) + len(self.failed)


class SubscriptionService:
    """
    Service for subscription management.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sub_repo = SubscriptionRepository(session)
        self.feed_repo = FeedRepository(session)
        self.feed_service = FeedService(session)
        self.channel_settings_repo = ChannelSettingsRepository(session)
        self.settings = get_settings()

    async def subscribe(
        self,
        platform: str,
        user_id: str,
        channel_id: str,
        feed_url: str,
        guild_id: str | None = None,
    ) -> SubscribeResult:
        """
        Subscribe a channel to a feed.

        This will:
        1. Add the feed if it doesn't exist
        2. Create the subscription

        Args:
            platform: Platform name (discord, telegram)
            user_id: User ID who created the subscription
            channel_id: Channel/chat ID
            feed_url: RSS feed URL
            guild_id: Guild/server ID (for Discord)

        Returns:
            SubscribeResult with subscription object
        """
        # Check quota
        if self.settings.max_feeds_per_channel > 0:
            count = await self.sub_repo.count_channel_subscriptions(platform, channel_id)
            if count >= self.settings.max_feeds_per_channel:
                return SubscribeResult(
                    success=False,
                    message=f"Maximum feeds ({self.settings.max_feeds_per_channel}) reached",
                )

        # Add or get feed
        add_result = await self.feed_service.add_feed(feed_url)
        if not add_result.success:
            return SubscribeResult(
                success=False,
                message=add_result.message,
            )

        feed = add_result.feed
        # add_feed succeeds only with a resolved feed attached
        assert feed is not None

        # Inherit the channel's persisted defaults (ChannelSettings) for a
        # NEW subscription. A recorded preference wins; otherwise silent
        # falls back to the legacy all-existing-subs-silent heuristic and
        # language/translate to the model defaults. Per-feed overrides
        # still apply afterwards — inheritance happens once, here.
        defaults = await self.channel_settings_repo.get(platform, channel_id)
        if defaults is not None and defaults.default_silent is not None:
            inherit_silent = defaults.default_silent
        else:
            inherit_silent = await self._channel_silent_default(platform, channel_id)
        inherit_translate = (
            defaults.default_translate
            if defaults is not None and defaults.default_translate is not None
            else True
        )
        inherit_language = (
            defaults.default_language
            if defaults is not None and defaults.default_language is not None
            else "zh-CN"
        )

        # Create subscription
        subscription, created = await self.sub_repo.get_or_create_subscription(
            platform=platform,
            user_id=user_id,
            channel_id=channel_id,
            feed_id=feed.id,
            guild_id=guild_id,
            silent=inherit_silent,
            translate=inherit_translate,
            target_language=inherit_language,
        )

        if not created:
            return SubscribeResult(
                success=True,
                subscription=subscription,
                feed=feed,
                message="Already subscribed to this feed",
                is_new=False,
            )

        # Seed SentEntry with all but the single most-recent entry. That
        # entry stays unsent so the user gets one preview article shortly
        # after subscribing (delivered by Dispatcher.schedule_preview post-commit),
        # instead of waiting up to a full FETCH_INTERVAL for the first message.
        seeded = await self.sub_repo.seed_sent_entries(
            subscription_id=subscription.id,
            feed_id=feed.id,
            keep_latest=1,
        )

        logger.info(
            f"New subscription: {platform}/{channel_id} -> {feed_url} "
            f"(seeded {seeded} back-catalog entries as sent; 1 kept for preview"
            f"{'; silent inherited' if inherit_silent else ''})"
        )

        message = f"Subscribed to {feed.title or feed_url}"
        if inherit_silent:
            message += " (silent mode inherited from channel)"

        return SubscribeResult(
            success=True,
            subscription=subscription,
            feed=feed,
            message=message,
            is_new=True,
        )

    async def _channel_silent_default(self, platform: str, channel_id: str) -> bool:
        """Legacy silent-inheritance heuristic: True iff every existing
        active subscription in this channel is silent. Only consulted
        when the channel has no recorded default_silent (ChannelSettings)
        — an explicit preference always wins. Empty channel returns
        False.
        """
        subs = await self.sub_repo.get_channel_subscriptions(platform, channel_id)
        if not subs:
            return False
        return all(s.silent for s in subs)

    async def unsubscribe(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
    ) -> UnsubscribeResult:
        """
        Unsubscribe a channel from a feed.

        Args:
            platform: Platform name
            channel_id: Channel/chat ID
            feed_url: RSS feed URL

        Returns:
            UnsubscribeResult
        """
        # Resolve gh:/yt:/… shortcuts the same way add_feed did, so a user can
        # manage a feed with the exact string they subscribed with (the stored
        # URL is the expanded form). No-op for ordinary URLs.
        feed_url = expand_source_shortcut(feed_url)

        # Find feed
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return UnsubscribeResult(
                success=False,
                message="Feed not found",
            )

        # Delete subscription
        deleted = await self.sub_repo.delete_subscription(
            platform=platform,
            channel_id=channel_id,
            feed_id=feed.id,
        )

        if not deleted:
            return UnsubscribeResult(
                success=False,
                message="Subscription not found",
            )

        logger.info(f"Unsubscribed: {platform}/{channel_id} from {feed_url}")

        return UnsubscribeResult(
            success=True,
            message=f"Unsubscribed from {feed.title or feed_url}",
        )

    async def pause_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
    ) -> SubscriptionActionResult:
        """Mark a subscription inactive. Dispatch skips it until resumed."""
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(success=False, message="Feed not found")
        updated = await self.sub_repo.deactivate_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not updated:
            return SubscriptionActionResult(success=False, message="Subscription not found")
        logger.info(f"Paused: {platform}/{channel_id} × {feed_url}")
        return SubscriptionActionResult(success=True, message=f"Paused {feed.title or feed_url}")

    async def resume_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
    ) -> SubscriptionActionResult:
        """Reactivate a previously paused subscription."""
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(success=False, message="Feed not found")
        updated = await self.sub_repo.activate_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not updated:
            return SubscriptionActionResult(success=False, message="Subscription not found")
        message = f"Resumed {feed.title or feed_url}"
        # The auto-disable notice tells users to run resume "once the source
        # is working again" — honor that: an auto-disabled feed has no other
        # user-reachable revival path (fetch skips inactive feeds, so its
        # error counter can never reset on its own).
        if not feed.is_active:
            feed.reactivate()
            message += " (feed re-enabled; it will be fetched next cycle)"
            logger.info(f"Reactivated auto-disabled feed on resume: {feed_url}")
        logger.info(f"Resumed: {platform}/{channel_id} × {feed_url}")
        return SubscriptionActionResult(success=True, message=message)

    async def resume_all_subscriptions(
        self,
        platform: str,
        channel_id: str,
    ) -> SubscriptionActionResult:
        """Reactivate every paused subscription in a channel, reviving any
        auto-disabled feeds they point at.

        Exists for the bulk-pause scenarios a per-URL resume can't
        reasonably cover: the bot being kicked and re-invited (ChannelGone
        deactivates the whole channel), or an operator un-mothballing a
        channel — dozens of URLs nobody wants to retype.
        """
        subs = await self.sub_repo.get_channel_subscriptions(
            platform, channel_id, include_inactive=True
        )
        if not subs:
            return SubscriptionActionResult(
                success=False, message="No subscriptions in this channel"
            )

        resumed = 0
        feeds_revived = 0
        for sub in subs:
            if not sub.is_active:
                sub.is_active = True
                resumed += 1
            if sub.feed and not sub.feed.is_active:
                sub.feed.reactivate()
                feeds_revived += 1

        digest_note = await self._digest_still_disabled_note(platform, channel_id)

        if resumed == 0 and feeds_revived == 0:
            return SubscriptionActionResult(
                success=True,
                message=f"All {len(subs)} subscription(s) already active" + digest_note,
            )

        message = f"Resumed {resumed} subscription(s)"
        if feeds_revived:
            message += f"; re-enabled {feeds_revived} auto-disabled feed(s)"
        message += digest_note
        logger.info(
            f"Resume all: {platform}/{channel_id} — {resumed} subs, "
            f"{feeds_revived} feeds revived"
        )
        return SubscriptionActionResult(success=True, message=message)

    async def _digest_still_disabled_note(self, platform: str, channel_id: str) -> str:
        """Appended to resume-all replies. ChannelGone deactivation disables
        the channel digest too, but resume-all can't re-enable it blindly —
        a digest the user turned off manually looks identical in the DB. A
        visible note beats a digest that silently never fires again."""
        from newsflow.repositories.digest_repository import ChannelDigestRepository

        config = await ChannelDigestRepository(self.session).get(platform, channel_id)
        if config is not None and not config.enabled:
            return (
                "\nNote: the channel digest is still disabled — "
                "use /digest enable to turn it back on."
            )
        return ""

    async def get_subscription_detail(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
        entry_limit: int = 5,
    ) -> SubscriptionDetail | None:
        """Fetch a single subscription with its feed and recent entries for
        a detailed status view. Returns None if the subscription doesn't exist.
        """
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return None
        sub = await self.sub_repo.get_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not sub:
            return None
        recent = await self.feed_repo.get_recent_entries(feed.id, entry_limit)
        return SubscriptionDetail(subscription=sub, feed=feed, recent_entries=list(recent))

    async def get_channel_subscriptions(
        self,
        platform: str,
        channel_id: str,
        include_inactive: bool = False,
    ) -> Sequence[Subscription]:
        """Get all subscriptions for a channel. User-facing listings pass
        include_inactive=True so paused subscriptions stay visible."""
        return await self.sub_repo.get_channel_subscriptions(
            platform, channel_id, include_inactive=include_inactive
        )

    async def get_subscription_by_id(self, subscription_id: int) -> Subscription | None:
        """Fetch one subscription (feed eager-loaded) by primary key.
        Used by inline-button UIs whose callback data can only carry an
        id — a URL doesn't fit in Telegram's 64-byte callback payload."""
        return await self.sub_repo.get_subscription_by_id(subscription_id)

    async def get_subscription_feeds(
        self,
        platform: str,
        channel_id: str,
    ) -> list[Feed]:
        """Get all feeds for a channel's subscriptions."""
        subs = await self.sub_repo.get_channel_subscriptions(platform, channel_id)
        return [sub.feed for sub in subs if sub.feed]

    async def update_settings(
        self,
        platform: str,
        channel_id: str,
        feed_url: str | None = None,
        translate: bool | None = None,
        target_language: str | None = None,
    ) -> int:
        """
        Update subscription settings; returns how many subscriptions were
        updated.

        If feed_url is None this is channel-wide: the preference is ALSO
        persisted as the channel default (ChannelSettings) so future
        subscriptions inherit it — which makes running this in a channel
        with zero subscriptions meaningful rather than an error.
        """
        if feed_url:
            feed_url = expand_source_shortcut(feed_url)
        else:
            # Channel-wide call → record the preference for future /adds.
            fields: dict[str, object] = {}
            if translate is not None:
                fields["default_translate"] = translate
            if target_language is not None:
                fields["default_language"] = target_language
            if fields:
                await self.channel_settings_repo.upsert(platform, channel_id, **fields)

        # Paused subscriptions get the new settings too — otherwise a channel
        # language change silently skips them and they resume with stale
        # settings later.
        subs = await self.sub_repo.get_channel_subscriptions(
            platform, channel_id, include_inactive=True
        )

        updated = 0
        for sub in subs:
            if feed_url and sub.feed.url != feed_url:
                continue

            await self.sub_repo.update_subscription_settings(
                subscription_id=sub.id,
                translate=translate,
                target_language=target_language,
            )
            updated += 1

        return updated

    async def set_feed_language(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
        language: str,
    ) -> SubscriptionActionResult:
        """Set the translation target language for a single subscription.

        Unlike update_settings(feed_url=None), which acts channel-wide,
        this is explicitly one-feed: different feeds in the same channel
        can have different target languages.
        """
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(success=False, message="Feed not found")
        sub = await self.sub_repo.get_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not sub:
            return SubscriptionActionResult(success=False, message="Subscription not found")
        await self.sub_repo.update_subscription_settings(
            subscription_id=sub.id, target_language=language
        )
        return SubscriptionActionResult(
            success=True,
            message=f"Language set to {language} for {feed.title or feed_url}",
        )

    async def set_feed_filter(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
        include_keywords: tuple[str, ...] = (),
        exclude_keywords: tuple[str, ...] = (),
        include_regex: str | None = None,
        exclude_regex: str | None = None,
    ) -> SubscriptionActionResult:
        """Set a keyword and/or regex filter on a subscription.

        Passing everything empty clears any existing filter. Regex
        patterns arrive pre-validated (adapters parse them via
        `parse_filter_field`).
        """
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(success=False, message="Feed not found")
        sub = await self.sub_repo.get_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not sub:
            return SubscriptionActionResult(success=False, message="Subscription not found")

        rule = FilterRule(
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            include_regex=include_regex,
            exclude_regex=exclude_regex,
        )
        await self.sub_repo.set_subscription_filter(
            subscription_id=sub.id, filter_rule=rule.to_json()
        )

        if rule.is_empty():
            msg = f"Filter cleared for {feed.title or feed_url}"
        else:
            parts = []
            if rule.include_regex:
                parts.append(f"include=/{rule.include_regex}/")
            elif rule.include_keywords:
                parts.append(f"include=[{', '.join(rule.include_keywords)}]")
            if rule.exclude_regex:
                parts.append(f"exclude=/{rule.exclude_regex}/")
            elif rule.exclude_keywords:
                parts.append(f"exclude=[{', '.join(rule.exclude_keywords)}]")
            msg = f"Filter set for {feed.title or feed_url}: " + " · ".join(parts)
        return SubscriptionActionResult(success=True, message=msg)

    async def clear_feed_filter(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
    ) -> SubscriptionActionResult:
        """Remove any filter on a subscription."""
        return await self.set_feed_filter(
            platform=platform,
            channel_id=channel_id,
            feed_url=feed_url,
            include_keywords=(),
            exclude_keywords=(),
        )

    async def get_feed_filter(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
    ) -> FilterRule | None:
        """Return the current filter for a subscription, or None if there's
        no subscription matching (platform, channel_id, feed_url)."""
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return None
        sub = await self.sub_repo.get_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not sub:
            return None
        return FilterRule.from_json(sub.filter_rule)

    async def set_feed_silent(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
        silent: bool,
    ) -> SubscriptionActionResult:
        """Toggle silent mode on a single subscription. Silent subs don't
        push instant messages to the channel, but their entries still flow
        into the digest pipeline."""
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(success=False, message="Feed not found")
        updated = await self.sub_repo.set_silent(
            platform=platform,
            channel_id=channel_id,
            feed_id=feed.id,
            silent=silent,
        )
        if not updated:
            return SubscriptionActionResult(success=False, message="Subscription not found")
        state = "on" if silent else "off"
        logger.info(f"Silent {state}: {platform}/{channel_id} × {feed_url}")
        return SubscriptionActionResult(
            success=True,
            message=f"Silent mode {state} for {feed.title or feed_url}",
        )

    async def set_channel_silent(
        self,
        platform: str,
        channel_id: str,
        silent: bool,
    ) -> SubscriptionActionResult:
        """Bulk-toggle silent on every subscription in this channel and
        persist it as the channel default so future subscriptions inherit
        it (this is what makes `/silent on` in an empty channel stick).
        Reports how many rows actually changed; zero is fine."""
        await self.channel_settings_repo.upsert(platform, channel_id, default_silent=silent)
        flipped = await self.sub_repo.set_channel_silent(
            platform=platform, channel_id=channel_id, silent=silent
        )
        state = "on" if silent else "off"
        logger.info(f"Channel silent {state}: {platform}/{channel_id} ({flipped} flipped)")
        if flipped == 0:
            return SubscriptionActionResult(
                success=True,
                message=f"Silent {state} saved as the channel default "
                f"(no existing subscriptions changed)",
            )
        return SubscriptionActionResult(
            success=True,
            message=f"Silent {state} on {flipped} subscription(s); saved as channel default",
        )

    async def set_feed_translate(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
        enabled: bool,
    ) -> SubscriptionActionResult:
        """Toggle translation for one subscription (see set_feed_language for
        rationale)."""
        feed_url = expand_source_shortcut(feed_url)
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(success=False, message="Feed not found")
        sub = await self.sub_repo.get_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not sub:
            return SubscriptionActionResult(success=False, message="Subscription not found")
        await self.sub_repo.update_subscription_settings(subscription_id=sub.id, translate=enabled)
        state = "enabled" if enabled else "disabled"
        return SubscriptionActionResult(
            success=True,
            message=f"Translation {state} for {feed.title or feed_url}",
        )

    async def export_opml(self, platform: str, channel_id: str) -> str:
        """Dump this channel's subscriptions as an OPML 2.0 document.
        Includes paused subscriptions — an export is a backup, and silently
        dropping paused feeds would lose them on re-import."""
        subs = await self.sub_repo.get_channel_subscriptions(
            platform, channel_id, include_inactive=True
        )
        entries = [
            OpmlEntry(
                url=sub.feed.url,
                title=sub.feed.title or None,
                html_url=sub.feed.site_url,
            )
            for sub in subs
        ]
        return build_opml(entries, title=f"NewsFlow Subscriptions ({platform})")

    async def import_opml(
        self,
        platform: str,
        user_id: str,
        channel_id: str,
        opml_content: str,
        guild_id: str | None = None,
    ) -> OpmlImportResult:
        """Parse an OPML document and bulk-subscribe. Preview dispatch is
        intentionally NOT triggered here — a 20-feed import would otherwise
        spam the channel with 20 preview articles at once. Users see new
        content on the next dispatch cycle like any other subscription.
        """
        try:
            entries = parse_opml(opml_content)
        except OpmlParseError as e:
            return OpmlImportResult(
                added=[],
                already_subscribed=[],
                failed=[("<opml>", str(e))],
            )

        result = OpmlImportResult(added=[], already_subscribed=[], failed=[])
        for entry in entries:
            sub_result = await self.subscribe(
                platform=platform,
                user_id=user_id,
                channel_id=channel_id,
                feed_url=entry.url,
                guild_id=guild_id,
            )
            if sub_result.success:
                if sub_result.is_new:
                    result.added.append(entry.url)
                else:
                    result.already_subscribed.append(entry.url)
            else:
                result.failed.append((entry.url, sub_result.message))

        logger.info(
            f"OPML import ({platform}/{channel_id}): "
            f"{len(result.added)} added, "
            f"{len(result.already_subscribed)} existing, "
            f"{len(result.failed)} failed"
        )
        return result

    async def get_unsent_entries(
        self,
        subscription_id: int,
        limit: int = 10,
    ) -> Sequence[FeedEntry]:
        """Get entries that haven't been sent to this subscription."""
        return await self.sub_repo.get_unsent_entries_for_subscription(subscription_id, limit)

    async def get_all_active_subscriptions(self) -> Sequence[Subscription]:
        """Get all active subscriptions."""
        return await self.sub_repo.get_all_active_subscriptions()

    async def cleanup_old_sent_entries(self, days: int = 7) -> int:
        """Cleanup old sent entry records."""
        count = await self.sub_repo.cleanup_old_sent_entries(days)
        logger.info(f"Cleaned up {count} old sent entry records")
        return count
