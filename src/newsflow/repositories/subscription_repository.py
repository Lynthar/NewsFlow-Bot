"""
Subscription repository for database operations.
"""

import logging
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from newsflow.config import get_settings
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories._result import rowcount

if TYPE_CHECKING:
    from newsflow.models.feed import FeedEntry

logger = logging.getLogger(__name__)


def _unsent(
    subscription_id: InstrumentedAttribute[int] | int, feed_id: InstrumentedAttribute[int] | int
) -> list[ColumnElement[bool]]:
    """FeedEntry conditions for the queue a subscription delivers from: entries of its feed
    with no SentEntry for it, inside the publish-age window. `published_at IS NULL` always
    passes — some feeds carry no date and we'd rather deliver than silently drop."""
    from newsflow.models.feed import FeedEntry

    # NOT EXISTS rather than NOT IN, to avoid the NULL-in-list trap.
    sent_exists = (
        select(SentEntry.id)
        .where(
            SentEntry.subscription_id == subscription_id,
            SentEntry.feed_id == FeedEntry.feed_id,
            SentEntry.guid == FeedEntry.guid,
        )
        .exists()
    )
    conditions = [FeedEntry.feed_id == feed_id, ~sent_exists]
    max_age_days = get_settings().max_entry_publish_age_days
    if max_age_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        conditions.append(or_(FeedEntry.published_at.is_(None), FeedEntry.published_at >= cutoff))
    return conditions


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

    async def migrate_channel(self, platform: str, old_channel_id: str, new_channel_id: str) -> int:
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
            conflict = await self.get_subscription(platform, new_channel_id, sub.feed_id)
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
            conditions.append(Subscription.is_active.is_(True))
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
            conditions.append(Subscription.is_active.is_(True))
        result = await self.session.execute(select(Subscription).where(*conditions))
        return result.scalars().all()

    async def get_all_active_subscriptions(self) -> Sequence[Subscription]:
        """Get all active subscriptions with their feeds."""
        result = await self.session.execute(
            select(Subscription)
            .options(selectinload(Subscription.feed))
            .where(Subscription.is_active.is_(True))
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
        message_thread_id: int | None = None,
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
            message_thread_id=message_thread_id,
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
        translate: bool = True,
        target_language: str = "zh-CN",
        message_thread_id: int | None = None,
    ) -> tuple[Subscription, bool]:
        """
        Get existing subscription or create new one.

        `silent` / `translate` / `target_language` / `message_thread_id`
        are applied only when a new subscription is created (typically the
        channel defaults — see ChannelSettings — plus the forum topic the
        command ran in). An existing subscription's preferences are
        preserved: re-subscribing won't flip them back.

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
            translate=translate,
            target_language=target_language,
            message_thread_id=message_thread_id,
        )
        return subscription, True

    async def update_subscription(self, subscription_id: int, **values: Any) -> None:
        """Set columns on one subscription. None is written as NULL — it clears the
        column — so drop "leave unchanged" keys before calling."""
        if values:
            await self.session.execute(
                update(Subscription).where(Subscription.id == subscription_id).values(**values)
            )

    async def update_channel_subscriptions(
        self,
        platform: str,
        channel_id: str,
        *,
        feed_id: int | None = None,
        skip_owners: Collection[str] = (),
        **values: Any,
    ) -> int:
        """Set columns on every subscription of a channel, paused ones included, or only
        on its subscription to `feed_id`; rows whose platform_user_id is in `skip_owners`
        are left alone. Returns how many rows matched."""
        if not values:
            return 0
        stmt = update(Subscription).where(
            Subscription.platform == platform,
            Subscription.platform_channel_id == channel_id,
            Subscription.platform_user_id.not_in(skip_owners),
        )
        if feed_id is not None:
            stmt = stmt.where(Subscription.feed_id == feed_id)
        return rowcount(await self.session.execute(stmt.values(**values)))

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
                Subscription.is_active.is_(True),
            )
            .values(is_active=False)
        )
        return rowcount(result)

    async def set_channel_silent(
        self,
        platform: str,
        channel_id: str,
        silent: bool,
        skip_owners: Collection[str] = (),
    ) -> int:
        """Bulk-toggle silent on every subscription in a channel except those whose
        platform_user_id is in `skip_owners`. Returns the number of rows whose state
        actually flipped (rows already in the target state are not counted)."""
        result = await self.session.execute(
            update(Subscription)
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.platform_user_id.not_in(skip_owners),
                Subscription.silent != silent,
            )
            .values(silent=silent)
        )
        return rowcount(result)

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
        return rowcount(result) > 0

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
                Subscription.is_active.is_(True),
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
        undeliverable: bool = False,
    ) -> SentEntry:
        """Record that a subscription has processed a (feed, guid) pair.

        Identifying by (feed_id, guid) rather than FeedEntry.id is the
        whole point of the post-2026-05-08 schema: the dedupe signal
        survives FeedEntry cleanup, so re-ingestion of the same guid
        doesn't re-deliver to channels that already saw it.

        `was_filtered=True` means the entry matched the subscription's
        filter rule out and was NOT actually delivered — we still persist
        a row so the dispatcher doesn't keep re-evaluating it forever.
        `undeliverable=True` means the platform kept refusing it and dispatch
        gave up; digests exclude those rows.
        """
        sent = SentEntry(
            subscription_id=subscription_id,
            feed_id=feed_id,
            guid=guid,
            was_filtered=was_filtered,
            undeliverable=undeliverable,
        )
        self.session.add(sent)
        await self.session.flush()
        return sent

    async def seed_predating_entries(
        self, feed_id: int, subscription_ids: Sequence[int] | None = None
    ) -> int:
        """Mark as seeded each entry of `feed_id` published before the subscription was
        created (undated ones too), for `subscription_ids` or all the feed's subscriptions,
        skipping entries a subscription already has a record for. Returns rows added."""
        from newsflow.models.feed import FeedEntry

        already = exists().where(
            SentEntry.subscription_id == Subscription.id,
            SentEntry.feed_id == FeedEntry.feed_id,
            SentEntry.guid == FeedEntry.guid,
        )
        stmt = (
            select(Subscription.id, FeedEntry.guid)
            .join(FeedEntry, FeedEntry.feed_id == Subscription.feed_id)
            .where(
                Subscription.feed_id == feed_id,
                or_(
                    FeedEntry.published_at.is_(None),
                    FeedEntry.published_at < Subscription.created_at,
                ),
                ~already,
            )
        )
        if subscription_ids is not None:
            stmt = stmt.where(Subscription.id.in_(subscription_ids))
        pairs = (await self.session.execute(stmt)).all()
        self.session.add_all(
            SentEntry(subscription_id=sub_id, feed_id=feed_id, guid=guid, seeded=True)
            for sub_id, guid in pairs
        )
        await self.session.flush()
        return len(pairs)

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
    ) -> Sequence["FeedEntry"]:
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

        # Oldest first: newest belongs at the bottom of the chat, and newest-first let
        # fresh entries permanently squeeze out older ones until retention dropped them.
        # Undated entries sort first; id breaks ties deterministically.
        result = await self.session.execute(
            select(FeedEntry)
            .where(*_unsent(subscription_id, subscription.feed_id))
            .order_by(
                FeedEntry.published_at.asc().nullsfirst(),
                FeedEntry.id.asc(),
            )
            .limit(limit)
        )
        return result.scalars().all()

    async def count_unsent_entries_for_subscription(self, subscription_id: int) -> int:
        """Size of the deliverable backlog — same predicate as
        get_unsent_entries_for_subscription (NOT EXISTS + publish-age
        window), just counted. Surfaces in /feed status so a stalled or
        rate-limited channel is visible instead of "randomly missing"."""
        from newsflow.models.feed import FeedEntry

        subscription = await self.get_subscription_by_id(subscription_id)
        if not subscription:
            return 0
        count = await self.session.scalar(
            select(func.count())
            .select_from(FeedEntry)
            .where(*_unsent(subscription_id, subscription.feed_id))
        )
        return int(count or 0)

    async def count_unsent_expiring(self, days: int) -> list[tuple[int, str, str, int]]:
        """Per active subscription, its queued entries that `cleanup_old_entries(days)`
        would delete now: (id, platform, channel id, count). Only non-zero counts."""
        from newsflow.models.feed import FeedEntry
        from newsflow.repositories.feed_repository import expired_entries

        result = await self.session.execute(
            select(
                Subscription.id,
                Subscription.platform,
                Subscription.platform_channel_id,
                func.count(FeedEntry.id),
            )
            .join(FeedEntry, FeedEntry.feed_id == Subscription.feed_id)
            .where(
                Subscription.is_active.is_(True),
                *_unsent(Subscription.id, Subscription.feed_id),
                *expired_entries(days, datetime.now(UTC)),
            )
            .group_by(Subscription.id, Subscription.platform, Subscription.platform_channel_id)
        )
        return [(row[0], row[1], row[2], int(row[3])) for row in result.all()]

    async def add_dropped_unsent(self, counts: dict[int, int]) -> None:
        """Add each subscription's count of entries cleanup deleted before delivery."""
        for sub_id, n in counts.items():
            await self.session.execute(
                update(Subscription)
                .where(Subscription.id == sub_id)
                .values(dropped_unsent=Subscription.dropped_unsent + n)
            )

    async def count_undeliverable(self, subscription_id: int) -> int:
        """Entries dispatch gave up on for this subscription, among the kept sent records."""
        count = await self.session.scalar(
            select(func.count()).where(
                SentEntry.subscription_id == subscription_id,
                SentEntry.undeliverable.is_(True),
            )
        )
        return int(count or 0)

    async def cleanup_old_sent_entries(self, days: int = 7) -> int:
        """Delete sent records last seen more than `days` ago, once their feed's latest
        full snapshot no longer lists the entry; for a feed without snapshots (a push
        source), once sent more than `days` ago."""
        from newsflow.models.feed import Feed

        cutoff = datetime.now(UTC) - timedelta(days=days)
        last_seen = func.coalesce(SentEntry.last_seen_at, SentEntry.sent_at)
        snapshot = select(Feed.last_full_fetch_at).where(Feed.id == SentEntry.feed_id)
        result = await self.session.execute(
            delete(SentEntry).where(
                # Implied by the next line (last_seen_at is never before sent_at);
                # stated so the sent_at index narrows the scan.
                SentEntry.sent_at < cutoff,
                last_seen < cutoff,
                or_(
                    snapshot.scalar_subquery().is_(None),
                    last_seen < snapshot.scalar_subquery(),
                ),
            )
        )
        return rowcount(result)
