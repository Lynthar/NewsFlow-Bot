"""
Subscription service - Business logic for subscription management.
"""

import logging
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.config import get_settings
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
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


class SubscriptionService:
    """
    Service for subscription management.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sub_repo = SubscriptionRepository(session)
        self.feed_repo = FeedRepository(session)
        self.feed_service = FeedService(session)
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

        # Create subscription
        subscription, created = await self.sub_repo.get_or_create_subscription(
            platform=platform,
            user_id=user_id,
            channel_id=channel_id,
            feed_id=feed.id,
            guild_id=guild_id,
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
            f"(seeded {seeded} back-catalog entries as sent; 1 kept for preview)"
        )

        return SubscribeResult(
            success=True,
            subscription=subscription,
            feed=feed,
            message=f"Subscribed to {feed.title or feed_url}",
            is_new=True,
        )

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
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(
                success=False, message="Feed not found"
            )
        updated = await self.sub_repo.deactivate_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not updated:
            return SubscriptionActionResult(
                success=False, message="Subscription not found"
            )
        logger.info(f"Paused: {platform}/{channel_id} × {feed_url}")
        return SubscriptionActionResult(
            success=True, message=f"Paused {feed.title or feed_url}"
        )

    async def resume_subscription(
        self,
        platform: str,
        channel_id: str,
        feed_url: str,
    ) -> SubscriptionActionResult:
        """Reactivate a previously paused subscription."""
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return SubscriptionActionResult(
                success=False, message="Feed not found"
            )
        updated = await self.sub_repo.activate_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not updated:
            return SubscriptionActionResult(
                success=False, message="Subscription not found"
            )
        logger.info(f"Resumed: {platform}/{channel_id} × {feed_url}")
        return SubscriptionActionResult(
            success=True, message=f"Resumed {feed.title or feed_url}"
        )

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
        feed = await self.feed_repo.get_feed_by_url(feed_url)
        if not feed:
            return None
        sub = await self.sub_repo.get_subscription(
            platform=platform, channel_id=channel_id, feed_id=feed.id
        )
        if not sub:
            return None
        recent = await self.feed_repo.get_recent_entries(feed.id, entry_limit)
        return SubscriptionDetail(
            subscription=sub, feed=feed, recent_entries=list(recent)
        )

    async def get_channel_subscriptions(
        self,
        platform: str,
        channel_id: str,
    ) -> Sequence[Subscription]:
        """Get all subscriptions for a channel."""
        return await self.sub_repo.get_channel_subscriptions(platform, channel_id)

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
    ) -> bool:
        """
        Update subscription settings.

        If feed_url is None, updates all subscriptions for the channel.
        """
        subs = await self.sub_repo.get_channel_subscriptions(platform, channel_id)

        if not subs:
            return False

        for sub in subs:
            if feed_url and sub.feed.url != feed_url:
                continue

            await self.sub_repo.update_subscription_settings(
                subscription_id=sub.id,
                translate=translate,
                target_language=target_language,
            )

        return True

    async def get_unsent_entries(
        self,
        subscription_id: int,
        limit: int = 10,
    ) -> Sequence[FeedEntry]:
        """Get entries that haven't been sent to this subscription."""
        return await self.sub_repo.get_unsent_entries_for_subscription(
            subscription_id, limit
        )

    async def mark_entry_sent(
        self,
        subscription_id: int,
        entry_id: int,
    ) -> None:
        """Mark an entry as sent to a subscription."""
        await self.sub_repo.mark_entry_sent(subscription_id, entry_id)

    async def is_entry_sent(
        self,
        subscription_id: int,
        entry_id: int,
    ) -> bool:
        """Check if an entry has been sent to a subscription."""
        return await self.sub_repo.is_entry_sent(subscription_id, entry_id)

    async def get_all_active_subscriptions(self) -> Sequence[Subscription]:
        """Get all active subscriptions."""
        return await self.sub_repo.get_all_active_subscriptions()

    async def cleanup_old_sent_entries(self, days: int = 7) -> int:
        """Cleanup old sent entry records."""
        count = await self.sub_repo.cleanup_old_sent_entries(days)
        logger.info(f"Cleaned up {count} old sent entry records")
        return count
