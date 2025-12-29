"""
Message dispatcher service.

Handles fetching feeds and dispatching new entries to subscribed channels.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.adapters.base import Message
from newsflow.config import get_settings
from newsflow.core.content_processor import get_source_name, process_content
from newsflow.models.base import get_session_factory
from newsflow.models.feed import FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.feed_repository import FeedRepository
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService
from newsflow.services.translation.factory import get_translation_service

if TYPE_CHECKING:
    from newsflow.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)


class MessageSender(Protocol):
    """Protocol for message sending."""

    async def send_message(self, channel_id: str, message: Message) -> bool:
        ...


@dataclass
class DispatchResult:
    """Result of a dispatch run."""
    feeds_fetched: int = 0
    new_entries: int = 0
    messages_sent: int = 0
    errors: int = 0


class Dispatcher:
    """
    Handles the main dispatch loop:
    1. Fetch all feeds
    2. Find new entries
    3. Send to subscribed channels
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._adapters: dict[str, MessageSender] = {}

    def register_adapter(self, platform: str, adapter: MessageSender) -> None:
        """Register a platform adapter for message sending."""
        self._adapters[platform] = adapter
        logger.info(f"Registered adapter for platform: {platform}")

    async def dispatch_once(self) -> DispatchResult:
        """
        Run one dispatch cycle.

        Returns:
            DispatchResult with statistics
        """
        result = DispatchResult()
        session_factory = get_session_factory()

        async with session_factory() as session:
            try:
                # Fetch all feeds
                feed_service = FeedService(session)
                fetch_results = await feed_service.fetch_all_feeds()

                result.feeds_fetched = len(fetch_results)

                # Collect new entries
                new_entries = []
                for fr in fetch_results:
                    if fr.success and fr.new_entries:
                        new_entries.extend(fr.new_entries)

                result.new_entries = len(new_entries)

                if not new_entries:
                    logger.debug("No new entries to dispatch")
                    return result

                # Dispatch to subscribers
                sub_repo = SubscriptionRepository(session)
                subscriptions = await sub_repo.get_all_active_subscriptions()

                for sub in subscriptions:
                    sent = await self._dispatch_to_subscription(
                        session, sub, sub_repo
                    )
                    result.messages_sent += sent

                await session.commit()

            except Exception as e:
                logger.exception(f"Dispatch error: {e}")
                result.errors += 1
                await session.rollback()

        return result

    async def _dispatch_to_subscription(
        self,
        session: AsyncSession,
        subscription: Subscription,
        sub_repo: SubscriptionRepository,
    ) -> int:
        """
        Dispatch new entries to a single subscription.

        Returns:
            Number of messages sent
        """
        # Get adapter for platform
        adapter = self._adapters.get(subscription.platform)
        if not adapter:
            logger.warning(f"No adapter for platform: {subscription.platform}")
            return 0

        # Get unsent entries
        entries = await sub_repo.get_unsent_entries_for_subscription(
            subscription.id, limit=10
        )

        if not entries:
            return 0

        sent_count = 0
        for entry in entries:
            try:
                # Create message (with translation if enabled)
                message = await self._create_message(entry, subscription, session)

                # Send
                success = await adapter.send_message(
                    subscription.platform_channel_id,
                    message,
                )

                if success:
                    # Mark as sent
                    await sub_repo.mark_entry_sent(subscription.id, entry.id)
                    sent_count += 1
                    logger.debug(
                        f"Sent entry {entry.id} to {subscription.platform}/{subscription.platform_channel_id}"
                    )
                else:
                    logger.warning(
                        f"Failed to send entry {entry.id} to {subscription.platform}/{subscription.platform_channel_id}"
                    )

                # Small delay between messages
                await asyncio.sleep(0.5)

            except Exception as e:
                logger.exception(f"Error sending entry {entry.id}: {e}")

        return sent_count

    async def _translate_entry(
        self,
        entry: FeedEntry,
        target_language: str,
        session: AsyncSession,
    ) -> tuple[str | None, str | None]:
        """
        Translate entry title and summary.

        Uses cached translations from the database if available.
        """
        # Check if already translated to this language
        if (
            entry.translation_language == target_language
            and entry.title_translated
        ):
            return entry.title_translated, entry.summary_translated

        # Get translation service
        translation_service = get_translation_service()
        if not translation_service:
            return None, None

        title_translated = None
        summary_translated = None

        try:
            # Translate title
            if entry.title:
                result = await translation_service.translate(
                    entry.title, target_language
                )
                if result.success:
                    title_translated = result.translated_text

            # Translate summary (truncate if too long)
            if entry.summary:
                summary_text = entry.summary[:1000] if len(entry.summary) > 1000 else entry.summary
                result = await translation_service.translate(
                    summary_text, target_language
                )
                if result.success:
                    summary_translated = result.translated_text

            # Cache translations in database
            if title_translated or summary_translated:
                feed_repo = FeedRepository(session)
                await feed_repo.update_entry_translation(
                    entry_id=entry.id,
                    title_translated=title_translated or "",
                    summary_translated=summary_translated or "",
                    language=target_language,
                )

        except Exception as e:
            logger.exception(f"Translation error for entry {entry.id}: {e}")

        return title_translated, summary_translated

    async def _create_message(
        self,
        entry: FeedEntry,
        subscription: Subscription,
        session: AsyncSession,
    ) -> Message:
        """Create a Message from a FeedEntry."""
        # Determine language for source name
        lang = "zh" if subscription.target_language.startswith("zh") else "en"
        source = get_source_name(entry.link, lang)

        title_translated = entry.title_translated
        summary_translated = entry.summary_translated

        # Translate if enabled for this subscription
        if subscription.translate:
            title_translated, summary_translated = await self._translate_entry(
                entry, subscription.target_language, session
            )

        return Message(
            title=entry.title,
            summary=entry.summary or "",
            link=entry.link,
            source=source,
            published_at=entry.published_at,
            image_url=entry.image_url,
            title_translated=title_translated,
            summary_translated=summary_translated,
        )

    async def run_dispatch_loop(self, interval_minutes: int | None = None) -> None:
        """
        Run the dispatch loop continuously.

        Args:
            interval_minutes: Override the default fetch interval
        """
        interval = interval_minutes or self.settings.fetch_interval_minutes
        logger.info(f"Starting dispatch loop with {interval} minute interval")

        while True:
            try:
                result = await self.dispatch_once()
                logger.info(
                    f"Dispatch complete: {result.feeds_fetched} feeds, "
                    f"{result.new_entries} new entries, "
                    f"{result.messages_sent} messages sent"
                )
            except Exception as e:
                logger.exception(f"Dispatch loop error: {e}")

            await asyncio.sleep(interval * 60)


# Global dispatcher instance
_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    """Get the global dispatcher instance."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
    return _dispatcher
