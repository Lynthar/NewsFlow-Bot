"""
Message dispatcher service.

Handles fetching feeds and dispatching new entries to subscribed channels.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.adapters.base import Message
from newsflow.config import get_settings
from newsflow.core.content_processor import (
    MAX_SUMMARY_LENGTH,
    clean_html,
    get_source_name,
    truncate_text,
)
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
    """Protocol for message sending. Adapters supply send_message (for
    structured feed entries), send_text (for system notifications like
    feed-auto-disabled notices), and is_connected (for HEALTHCHECK)."""

    async def send_message(self, channel_id: str, message: Message) -> bool:
        ...

    async def send_text(self, channel_id: str, text: str) -> bool:
        ...

    def is_connected(self) -> bool:
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
        # Platforms we expect to register before the first dispatch round.
        # Cleared as each adapter registers; guards against silently dropping
        # the first round of messages while bots are still connecting.
        self._expected_platforms: set[str] = set()
        if self.settings.discord_enabled:
            self._expected_platforms.add("discord")
        if self.settings.telegram_enabled:
            self._expected_platforms.add("telegram")
        self._ready_event = asyncio.Event()
        if not self._expected_platforms:
            self._ready_event.set()

    def register_adapter(self, platform: str, adapter: MessageSender) -> None:
        """Register a platform adapter for message sending."""
        self._adapters[platform] = adapter
        self._expected_platforms.discard(platform)
        if not self._expected_platforms:
            self._ready_event.set()
        logger.info(f"Registered adapter for platform: {platform}")

    def heartbeat_path(self, name: str = "dispatch") -> Path:
        """Per-task heartbeat file. HEALTHCHECK reads the directory and
        fails if any file hasn't been touched within the freshness window,
        so each long-running task writes its own."""
        return self.settings.data_dir / "heartbeat" / name

    def _write_heartbeat(self, name: str = "dispatch") -> None:
        """Touch a named heartbeat file. Failures are swallowed — heartbeat
        must never be the thing that breaks a task."""
        try:
            path = self.heartbeat_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except OSError as e:
            logger.debug(f"Heartbeat write failed ({name}): {e}")

    async def wait_for_adapters(self, timeout: float = 60.0) -> bool:
        """Wait until all expected adapters have registered.

        Returns True if ready, False on timeout. On timeout the dispatch loop
        should still run — a stuck platform shouldn't block the others.
        """
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"Timed out waiting for adapters; missing: {self._expected_platforms}"
            )
            return False

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

                if new_entries:
                    sub_repo = SubscriptionRepository(session)
                    subscriptions = await sub_repo.get_all_active_subscriptions()
                    for sub in subscriptions:
                        sent = await self._dispatch_to_subscription(
                            session, sub, sub_repo
                        )
                        result.messages_sent += sent
                    await session.commit()
                else:
                    logger.debug("No new entries to dispatch")

            except Exception as e:
                logger.exception(f"Dispatch error: {e}")
                result.errors += 1
                await session.rollback()

        # Heartbeat reflects "dispatch loop iterated", not "dispatch succeeded".
        # A handled error still counts — the loop is alive and trying.
        self._write_heartbeat("dispatch")
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

                # Small smoothing pause between sends. Platform-level rate
                # limiting is enforced by the libraries (discord.py internal
                # buckets; Telegram AIORateLimiter); this is just a nudge to
                # avoid bursty spikes when many entries are due at once.
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.exception(f"Error sending entry {entry.id}: {e}")

        return sent_count

    async def _translate_entry(
        self,
        entry: FeedEntry,
        target_language: str,
        session: AsyncSession,
        plain_summary: str,
    ) -> tuple[str | None, str | None]:
        """
        Translate entry title and summary.

        Uses cached translations from the database if available. Caller passes
        `plain_summary` (HTML already stripped) so we never translate markup.
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

            # Translate summary (cap length to keep token usage bounded)
            if plain_summary:
                summary_text = plain_summary[:1000]
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

        # Feeds like hnrss.org embed raw HTML (<p>, <a href>) in summary /
        # description fields. Discord/Telegram don't render HTML, so we'd
        # ship it to the user as literal angle brackets. Strip here, prefer
        # `content` (fuller) over `summary`.
        raw_body = entry.content or entry.summary or ""
        plain_summary, _images = clean_html(raw_body)
        plain_summary = truncate_text(plain_summary, MAX_SUMMARY_LENGTH)

        title_translated = entry.title_translated
        summary_translated = entry.summary_translated

        # Translate if enabled for this subscription. Pass the cleaned
        # summary so we don't spend tokens translating <p> tags or get back
        # a translation that still contains HTML.
        if subscription.translate:
            title_translated, summary_translated = await self._translate_entry(
                entry, subscription.target_language, session, plain_summary
            )

        return Message(
            title=entry.title,
            summary=plain_summary,
            link=entry.link,
            source=source,
            published_at=entry.published_at,
            image_url=entry.image_url,
            title_translated=title_translated,
            summary_translated=summary_translated,
        )

    async def run_platform_monitor(self, interval_seconds: int = 30) -> None:
        """Periodically touch a per-platform heartbeat while its adapter
        reports a live connection. HEALTHCHECK reads these alongside the
        dispatch/cleanup heartbeats, so a hung platform connection shows up
        independently of the dispatch loop still iterating.
        """
        await self.wait_for_adapters(timeout=60.0)
        logger.info("Starting platform monitor")

        while True:
            for platform, adapter in list(self._adapters.items()):
                try:
                    connected = adapter.is_connected()
                except Exception:
                    logger.exception(
                        f"adapter.is_connected() raised for {platform}"
                    )
                    connected = False

                if connected:
                    self._write_heartbeat(platform)

            await asyncio.sleep(interval_seconds)

    async def dispatch_subscription(self, subscription_id: int) -> int:
        """Dispatch unsent entries for one subscription, in its own session.

        Used for the post-subscribe preview path: after a user runs /add, we
        immediately deliver the single most-recent entry (seed kept it unsent)
        so they don't have to wait a full FETCH_INTERVAL to see any content.

        Returns:
            Number of messages successfully sent.
        """
        session_factory = get_session_factory()
        async with session_factory() as session:
            sub_repo = SubscriptionRepository(session)
            sub = await sub_repo.get_subscription_by_id(subscription_id)
            if sub is None or not sub.is_active:
                return 0
            adapter = self._adapters.get(sub.platform)
            if adapter is None:
                logger.debug(
                    f"dispatch_subscription: no adapter for {sub.platform}; "
                    f"preview deferred to regular dispatch loop"
                )
                return 0

            sent = await self._dispatch_to_subscription(session, sub, sub_repo)
            await session.commit()
            return sent

    async def notify_feed_deactivated(
        self, feed_id: int, feed_url: str, feed_title: str | None
    ) -> None:
        """Send a system message to all of a feed's subscribers (including
        paused ones) that the feed has been auto-disabled. Caller schedules
        this via asyncio.create_task after detecting the deactivation —
        identity is passed as args rather than re-read from DB so it works
        before the caller's transaction commits.
        """
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                sub_repo = SubscriptionRepository(session)
                subs = await sub_repo.get_feed_subscriptions(
                    feed_id, include_inactive=True
                )

            if not subs:
                return

            name = feed_title or feed_url
            text = (
                f"⚠️ The RSS feed \"{name}\" has been auto-disabled after "
                f"10 consecutive fetch errors. Use /feed resume <url> once "
                f"the source is working again, or /feed remove <url> to "
                f"clean up.\nURL: {feed_url}"
            )

            for sub in subs:
                adapter = self._adapters.get(sub.platform)
                if adapter is None:
                    continue
                try:
                    await adapter.send_text(sub.platform_channel_id, text)
                except Exception:
                    logger.exception(
                        f"Failed to send deactivation notice to "
                        f"{sub.platform}/{sub.platform_channel_id}"
                    )
        except Exception:
            logger.exception(f"notify_feed_deactivated({feed_id}) failed")

    async def schedule_preview(self, subscription_id: int) -> None:
        """Fire-and-forget wrapper for dispatch_subscription, safe to use
        with `asyncio.create_task(...)` from a slash command handler. Never
        raises — preview failures are logged but don't affect the user's ack.
        """
        try:
            sent = await self.dispatch_subscription(subscription_id)
            if sent:
                logger.info(
                    f"Preview: delivered {sent} entry/entries to subscription {subscription_id}"
                )
        except Exception:
            logger.exception(
                f"Preview dispatch failed for subscription {subscription_id}"
            )

    async def run_cleanup_loop(self) -> None:
        """Periodically delete old feed entries and sent-entry records.

        Runs forever on `settings.cleanup_interval_hours`, deleting anything
        older than `settings.entry_retention_days`.
        """
        interval_seconds = self.settings.cleanup_interval_hours * 3600
        retention_days = self.settings.entry_retention_days

        logger.info(
            f"Starting cleanup loop (every {self.settings.cleanup_interval_hours}h, "
            f"retaining {retention_days} days)"
        )

        # Delay first run so startup logs stay clean and DB is definitely up.
        await asyncio.sleep(60)

        while True:
            try:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    feed_repo = FeedRepository(session)
                    sub_repo = SubscriptionRepository(session)

                    entries_deleted = await feed_repo.cleanup_old_entries(retention_days)
                    sent_deleted = await sub_repo.cleanup_old_sent_entries(retention_days)
                    await session.commit()

                    logger.info(
                        f"Cleanup: deleted {entries_deleted} old entries, "
                        f"{sent_deleted} old sent records"
                    )
            except Exception:
                logger.exception("Cleanup loop error")

            # Mark cleanup alive regardless of whether the iteration did work.
            self._write_heartbeat("cleanup")
            await asyncio.sleep(interval_seconds)

    async def run_dispatch_loop(self, interval_minutes: int | None = None) -> None:
        """
        Run the dispatch loop continuously.

        Args:
            interval_minutes: Override the default fetch interval
        """
        interval = interval_minutes or self.settings.fetch_interval_minutes

        # Don't start dispatching until adapters are ready — otherwise the
        # first round finds no registered adapter and no-ops (users would
        # wait a full interval for the first message).
        await self.wait_for_adapters(timeout=60.0)

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
