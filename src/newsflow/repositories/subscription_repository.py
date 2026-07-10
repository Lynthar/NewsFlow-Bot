"""
Subscription repository for database operations.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from newsflow.config import get_settings
from newsflow.models.feed import Feed
from newsflow.models.subscription import SentEntry, Subscription

logger = logging.getLogger(__name__)


class SubscriptionRepository:
    """
    Repository for Subscription operations.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ===== Subscription Operations =====

    async def get_subscription_by_id(self, subscription_id: int) -> Subscription | None:
        """Get a subscription by ID."""
        result = await self.session.execute(
            select(Subscription)
            .options(selectinload(Subscription.feed))
            .where(Subscription.id == subscription_id)
        )
        return result.scalar_one_or_none()

    async def get_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_id: int,
    ) -> Subscription | None:
        """Get a specific subscription."""
        result = await self.session.execute(
            select(Subscription)
            .options(selectinload(Subscription.feed))
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.feed_id == feed_id,
            )
        )
        return result.scalar_one_or_none()

    async def migrate_channel(
        self, platform: str, old_channel_id: str, new_channel_id: str
    ) -> int:
        """Repoint every subscription for (platform, old_channel_id) at
        new_channel_id.

        Telegram group→supergroup migrations keep members and history but
        issue a brand-new chat id; rewriting the rows in place (same id, so
        SentEntry dedupe history rides along) keeps delivery seamless. If
        the new id already has a subscription for the same feed (bot was
        re-added and the feed re-subscribed before we saw the migration),
        the old row is dropped in favor of the incumbent. Returns the
        number of rows repointed.
        """
        result = await self.session.execute(
            select(Subscription).where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == old_channel_id,
            )
        )
        moved = 0
        for sub in result.scalars().all():
            conflict = await self.get_subscription(
                platform, new_channel_id, sub.feed_id
            )
            if conflict is not None:
                await self.session.delete(sub)
                continue
            sub.platform_channel_id = new_channel_id
            moved += 1
        await self.session.flush()
        return moved

    async def get_channel_subscriptions(
        self,
        platform: str,
        channel_id: str,
        include_inactive: bool = False,
    ) -> Sequence[Subscription]:
        """Get all subscriptions for a channel.

        By default only active ones (what dispatch and the silent-inherit
        heuristic use). Pass include_inactive=True for user-facing views —
        /feed list and OPML export must show paused subscriptions, or
        pausing makes them (and their URLs) unfindable and thus
        unresumable. Ordered by id so pagination is stable across calls.
        """
        conditions = [
            Subscription.platform == platform,
            Subscription.platform_channel_id == channel_id,
        ]
        if not include_inactive:
            conditions.append(Subscription.is_active == True)
        result = await self.session.execute(
            select(Subscription)
            .options(selectinload(Subscription.feed))
            .where(*conditions)
            .order_by(Subscription.id)
        )
        return result.scalars().all()

    async def get_feed_subscriptions(
        self, feed_id: int, include_inactive: bool = False
    ) -> Sequence[Subscription]:
        """Get subscriptions for a feed.

        By default only active ones (what dispatch uses). Pass
        include_inactive=True when the caller wants paused subscribers too
        — e.g. for system notifications that every subscriber should see
        regardless of their pause state.
        """
        conditions = [Subscription.feed_id == feed_id]
        if not include_inactive:
            conditions.append(Subscription.is_active == True)
        result = await self.session.execute(
            select(Subscription).where(*conditions)
        )
        return result.scalars().all()

    async def get_all_active_subscriptions(self) -> Sequence[Subscription]:
        """Get all active subscriptions with their feeds."""
        result = await self.session.execute(
            select(Subscription)
            .options(selectinload(Subscription.feed))
            .where(Subscription.is_active == True)
        )
        return result.scalars().all()

    async def create_subscription(
        self,
        platform: str,
        user_id: str,
        channel_id: str,
        feed_id: int,
        guild_id: str | None = None,
        translate: bool = True,
        target_language: str = "zh-CN",
        silent: bool = False,
    ) -> Subscription:
        """Create a new subscription."""
        subscription = Subscription(
            platform=platform,
            platform_user_id=user_id,
            platform_channel_id=channel_id,
            platform_guild_id=guild_id,
            feed_id=feed_id,
            translate=translate,
            target_language=target_language,
            silent=silent,
        )
        self.session.add(subscription)
        await self.session.flush()
        await self.session.refresh(subscription)
        return subscription

    async def get_or_create_subscription(
        self,
        platform: str,
        user_id: str,
        channel_id: str,
        feed_id: int,
        guild_id: str | None = None,
        silent: bool = False,
    ) -> tuple[Subscription, bool]:
        """
        Get existing subscription or create new one.

        `silent` is applied only when a new subscription is created. An
        existing subscription's silent state is preserved (re-subscribing
        won't silently flip it back).

        Returns:
            Tuple of (subscription, created)
        """
        existing = await self.get_subscription(platform, channel_id, feed_id)
        if existing:
            # Reactivate if inactive
            if not existing.is_active:
                existing.is_active = True
                return existing, False
            return existing, False

        subscription = await self.create_subscription(
            platform=platform,
            user_id=user_id,
            channel_id=channel_id,
            feed_id=feed_id,
            guild_id=guild_id,
            silent=silent,
        )
        return subscription, True

    async def update_subscription_settings(
        self,
        subscription_id: int,
        translate: bool | None = None,
        target_language: str | None = None,
        show_summary: bool | None = None,
        show_image: bool | None = None,
    ) -> None:
        """Update subscription settings."""
        update_data = {}
        if translate is not None:
            update_data["translate"] = translate
        if target_language is not None:
            update_data["target_language"] = target_language
        if show_summary is not None:
            update_data["show_summary"] = show_summary
        if show_image is not None:
            update_data["show_image"] = show_image

        if update_data:
            await self.session.execute(
                update(Subscription)
                .where(Subscription.id == subscription_id)
                .values(**update_data)
            )

    async def set_subscription_filter(
        self,
        subscription_id: int,
        filter_rule: dict | None,
    ) -> None:
        """Set or clear the filter_rule column. `None` clears the filter."""
        await self.session.execute(
            update(Subscription)
            .where(Subscription.id == subscription_id)
            .values(filter_rule=filter_rule)
        )

    async def deactivate_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_id: int,
    ) -> bool:
        """Deactivate a subscription. Dispatch skips inactive subscriptions
        but the row is retained so it can be resumed without losing state."""
        result = await self.session.execute(
            update(Subscription)
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.feed_id == feed_id,
            )
            .values(is_active=False)
        )
        return result.rowcount > 0

    async def deactivate_channel(
        self,
        platform: str,
        channel_id: str,
    ) -> int:
        """Bulk-deactivate every active subscription for a channel.

        Called by the dispatcher when the adapter raises
        ChannelGoneError — the channel is permanently unreachable
        (deleted, bot kicked), so keeping subs active just burns API
        calls every dispatch cycle. Rows are retained so a future
        `/feed resume` can bring them back if the channel reappears
        (unlikely for Discord since snowflake ids are never reused,
        but harmless as a safety net). Returns the number of rows
        flipped — zero means an earlier caller in the same cycle
        already handled this channel.
        """
        result = await self.session.execute(
            update(Subscription)
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.is_active == True,  # noqa: E712
            )
            .values(is_active=False)
        )
        return result.rowcount

    async def activate_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_id: int,
    ) -> bool:
        """Reactivate a previously paused subscription."""
        result = await self.session.execute(
            update(Subscription)
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.feed_id == feed_id,
            )
            .values(is_active=True)
        )
        return result.rowcount > 0

    async def set_silent(
        self,
        platform: str,
        channel_id: str,
        feed_id: int,
        silent: bool,
    ) -> bool:
        """Toggle silent mode on a single subscription. Returns True if a
        row was actually updated, False if no matching subscription exists."""
        result = await self.session.execute(
            update(Subscription)
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.feed_id == feed_id,
            )
            .values(silent=silent)
        )
        return result.rowcount > 0

    async def set_channel_silent(
        self,
        platform: str,
        channel_id: str,
        silent: bool,
    ) -> int:
        """Bulk-toggle silent on every subscription in a channel. Returns
        the number of rows whose state actually flipped (rows already in
        the target state are not counted, thanks to the != predicate)."""
        result = await self.session.execute(
            update(Subscription)
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.silent != silent,
            )
            .values(silent=silent)
        )
        return result.rowcount

    async def delete_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_id: int,
    ) -> bool:
        """Delete a subscription."""
        result = await self.session.execute(
            delete(Subscription).where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.feed_id == feed_id,
            )
        )
        return result.rowcount > 0

    async def count_channel_subscriptions(
        self,
        platform: str,
        channel_id: str,
    ) -> int:
        """Count subscriptions for a channel."""
        from sqlalchemy import func
        result = await self.session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.is_active == True,
            )
        )
        return result.scalar_one()

    # ===== SentEntry Operations =====

    async def is_entry_sent(
        self,
        subscription_id: int,
        feed_id: int,
        guid: str,
    ) -> bool:
        """Check if a (feed, guid) pair has been sent to a subscription."""
        result = await self.session.execute(
            select(SentEntry).where(
                SentEntry.subscription_id == subscription_id,
                SentEntry.feed_id == feed_id,
                SentEntry.guid == guid,
            )
        )
        return result.scalar_one_or_none() is not None

    async def mark_entry_sent(
        self,
        subscription_id: int,
        feed_id: int,
        guid: str,
        was_filtered: bool = False,
    ) -> SentEntry:
        """Record that a subscription has processed a (feed, guid) pair.

        Identifying by (feed_id, guid) rather than FeedEntry.id is the
        whole point of the post-2026-05-08 schema: the dedupe signal
        survives FeedEntry cleanup, so re-ingestion of the same guid
        doesn't re-deliver to channels that already saw it.

        `was_filtered=True` means the entry matched the subscription's
        filter rule out and was NOT actually delivered — we still persist
        a row so the dispatcher doesn't keep re-evaluating it forever.
        """
        sent = SentEntry(
            subscription_id=subscription_id,
            feed_id=feed_id,
            guid=guid,
            was_filtered=was_filtered,
        )
        self.session.add(sent)
        await self.session.flush()
        return sent

    async def seed_sent_entries(
        self,
        subscription_id: int,
        feed_id: int,
        keep_latest: int = 0,
    ) -> int:
        """Seed SentEntry so a new subscription doesn't flood the channel
        with backlog. Entries ordered newest-first by published_at; the top
        `keep_latest` are left unsent (they'll be delivered on next dispatch
        as a preview). Remaining entries are marked sent with ``seeded=True``
        so the digest pipeline skips them — they were suppressed, never shown
        to the channel.

        Returns:
            Number of rows seeded (i.e. count of entries excluded from preview).
        """
        from newsflow.models.feed import FeedEntry

        stmt = (
            select(FeedEntry.guid)
            .where(FeedEntry.feed_id == feed_id)
            .order_by(
                FeedEntry.published_at.desc().nullslast(),
                FeedEntry.id.desc(),
            )
        )
        if keep_latest > 0:
            stmt = stmt.offset(keep_latest)

        result = await self.session.execute(stmt)
        guids = result.scalars().all()

        if not guids:
            return 0

        self.session.add_all(
            [
                SentEntry(
                    subscription_id=subscription_id,
                    feed_id=feed_id,
                    guid=guid,
                    # Mark as seeded, not delivered: these entries were never
                    # shown to the channel, so the digest must skip them.
                    seeded=True,
                )
                for guid in guids
            ]
        )
        await self.session.flush()
        return len(guids)

    async def get_unsent_entries_for_subscription(
        self,
        subscription_id: int,
        limit: int = 10,
    ) -> Sequence:
        """
        Get entries that haven't been sent to this subscription.

        Returns FeedEntry objects whose (feed_id, guid) does NOT appear
        in SentEntry for this subscription. Entries whose `published_at`
        is older than `settings.max_entry_publish_age_days` are filtered
        out so that feeds re-serving their archive don't push ancient
        articles to users. `published_at IS NULL` always passes — some
        feeds don't carry a date and we'd rather deliver than silently
        drop. `max_entry_publish_age_days = 0` disables the filter.
        """
        from newsflow.models.feed import FeedEntry

        subscription = await self.get_subscription_by_id(subscription_id)
        if not subscription:
            return []

        # NOT EXISTS join: rows in feed_entries with no matching SentEntry
        # for this subscription on (feed_id, guid). NOT EXISTS rather
        # than NOT IN to avoid the well-known NULL-in-list trap.
        sent_exists = (
            select(SentEntry.id)
            .where(
                SentEntry.subscription_id == subscription_id,
                SentEntry.feed_id == FeedEntry.feed_id,
                SentEntry.guid == FeedEntry.guid,
            )
            .exists()
        )

        conditions = [
            FeedEntry.feed_id == subscription.feed_id,
            ~sent_exists,
        ]

        max_age_days = get_settings().max_entry_publish_age_days
        if max_age_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            conditions.append(
                or_(
                    FeedEntry.published_at.is_(None),
                    FeedEntry.published_at >= cutoff,
                )
            )

        # Oldest first, for two reasons. Chronology: the newest article
        # should land at the bottom of the chat, not above older ones.
        # Backlog fairness: with more than `limit` pending, newest-first
        # let each cycle's fresh entries permanently squeeze out older
        # ones until retention silently dropped them — oldest-first
        # drains the backlog across cycles instead. Undated entries sort
        # first (can't age them; deliver rather than starve), id breaks
        # ties deterministically.
        result = await self.session.execute(
            select(FeedEntry)
            .where(*conditions)
            .order_by(
                FeedEntry.published_at.asc().nullsfirst(),
                FeedEntry.id.asc(),
            )
            .limit(limit)
        )
        return result.scalars().all()

    async def cleanup_old_sent_entries(self, days: int = 7) -> int:
        """Delete old sent entry records."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await self.session.execute(
            delete(SentEntry).where(SentEntry.sent_at < cutoff)
        )
        return result.rowcount
