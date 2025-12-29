"""
Subscription repository for database operations.
"""

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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

    async def get_channel_subscriptions(
        self,
        platform: str,
        channel_id: str,
    ) -> Sequence[Subscription]:
        """Get all subscriptions for a channel."""
        result = await self.session.execute(
            select(Subscription)
            .options(selectinload(Subscription.feed))
            .where(
                Subscription.platform == platform,
                Subscription.platform_channel_id == channel_id,
                Subscription.is_active == True,
            )
        )
        return result.scalars().all()

    async def get_feed_subscriptions(self, feed_id: int) -> Sequence[Subscription]:
        """Get all active subscriptions for a feed."""
        result = await self.session.execute(
            select(Subscription)
            .where(
                Subscription.feed_id == feed_id,
                Subscription.is_active == True,
            )
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
    ) -> tuple[Subscription, bool]:
        """
        Get existing subscription or create new one.

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

    async def deactivate_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_id: int,
    ) -> bool:
        """Deactivate a subscription."""
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
        entry_id: int,
    ) -> bool:
        """Check if an entry has been sent to a subscription."""
        result = await self.session.execute(
            select(SentEntry).where(
                SentEntry.subscription_id == subscription_id,
                SentEntry.entry_id == entry_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def mark_entry_sent(
        self,
        subscription_id: int,
        entry_id: int,
    ) -> SentEntry:
        """Mark an entry as sent to a subscription."""
        sent = SentEntry(
            subscription_id=subscription_id,
            entry_id=entry_id,
        )
        self.session.add(sent)
        await self.session.flush()
        return sent

    async def get_unsent_entries_for_subscription(
        self,
        subscription_id: int,
        limit: int = 10,
    ) -> Sequence:
        """
        Get entries that haven't been sent to this subscription.

        Returns FeedEntry objects that are not in SentEntry for this subscription.
        """
        from newsflow.models.feed import FeedEntry

        subscription = await self.get_subscription_by_id(subscription_id)
        if not subscription:
            return []

        # Subquery for sent entry IDs
        sent_subquery = (
            select(SentEntry.entry_id)
            .where(SentEntry.subscription_id == subscription_id)
            .scalar_subquery()
        )

        result = await self.session.execute(
            select(FeedEntry)
            .where(
                FeedEntry.feed_id == subscription.feed_id,
                FeedEntry.id.not_in(sent_subquery),
            )
            .order_by(FeedEntry.published_at.desc().nullslast())
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
