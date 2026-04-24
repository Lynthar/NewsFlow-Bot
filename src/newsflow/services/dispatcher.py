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
    dedup_summary,
    get_source_name,
    truncate_text,
)
from newsflow.core.filter import FilterRule
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
    feed-auto-disabled notices), send_text_pinned + unpin_message (for
    digest auto-pin — default implementations on BaseAdapter degrade to
    send-without-pin), and is_connected (for HEALTHCHECK)."""

    async def send_message(self, channel_id: str, message: Message) -> bool:
        ...

    async def send_text(self, channel_id: str, text: str) -> bool:
        ...

    async def send_text_pinned(
        self, channel_id: str, text: str
    ) -> tuple[bool, str | None]:
        ...

    async def unpin_message(
        self, channel_id: str, message_id: str
    ) -> bool:
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
        if self.settings.webhooks_enabled:
            self._expected_platforms.add("webhook")
        self._ready_event = asyncio.Event()
        if not self._expected_platforms:
            self._ready_event.set()
        # Strong refs for fire-and-forget background tasks. Without this the
        # event loop only holds weak refs and a task can be GC'd mid-run. See
        # https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
        self._background_tasks: set[asyncio.Task] = set()

    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Schedule `coro` as a fire-and-forget task, held by a strong ref
        until it completes. Use this instead of bare asyncio.create_task()
        anywhere the return value would otherwise be discarded."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

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
                else:
                    logger.debug("No new entries to dispatch")

                # Always commit so per-feed metadata written by fetch_all_feeds
                # (etag / last_modified / last_fetched_at / error_count /
                # next_retry_at) persists even when every feed returned 304 or
                # no new items. Without this the AsyncSession context manager
                # rolls back on exit, silently defeating the ETag cache,
                # exponential backoff, and the 10-errors auto-deactivate.
                await session.commit()

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

        # Decode the subscription's keyword filter once for this batch.
        # Empty rule matches everything (no-op filter).
        filter_rule = FilterRule.from_json(subscription.filter_rule)

        sent_count = 0
        for entry in entries:
            try:
                # Apply filter before the (potentially expensive) translation
                # and send path. Filtered entries are marked "processed" so
                # the loop doesn't re-evaluate them every dispatch cycle.
                if not filter_rule.is_empty():
                    haystack = f"{entry.title} {entry.summary or ''}"
                    if not filter_rule.matches(haystack):
                        await sub_repo.mark_entry_sent(
                            subscription.id, entry.id, was_filtered=True
                        )
                        logger.debug(
                            f"Entry {entry.id} filtered out for "
                            f"{subscription.platform}/{subscription.platform_channel_id}"
                        )
                        continue

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
        # Drop summaries that merely echo the title — common in Google
        # News wrappers ("Title  Source") and headline-only feeds. With
        # dedup done BEFORE translation, we also skip an API call on the
        # redundant text. See content_processor.dedup_summary for rules.
        plain_summary = dedup_summary(entry.title, plain_summary)

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

    def apply_digest_header(self, text: str, platform: str) -> str:
        """Prepend a visible header + platform-appropriate mention to
        digest text when DIGEST_MENTION_ON_DELIVERY is enabled.

        Shared between the scheduled digest path (`_tick_digests`) and
        the manual `/digest now` handlers in both Discord and Telegram
        adapters, so either trigger produces the same delivery shape.
        Without this shim the 3 paths had drifted — mention only fired
        on scheduled runs, not on manual test runs.

        Discord gets `@here` (requires bot "Mention Everyone" perm to
        actually notify). Telegram / webhook just get the visible
        header — no mention token since those platforms' notification
        model differs.
        """
        if not self.settings.digest_mention_on_delivery:
            return text
        if platform == "discord":
            return "@here 📰 **Digest**\n\n" + text
        return "📰 **Digest**\n\n" + text

    async def _send_text_split(
        self, adapter: "MessageSender", channel_id: str, text: str, chunk_size: int
    ) -> int:
        """Split long text on paragraph boundaries and send in chunks.
        Returns the number of successfully-sent chunks.
        """
        chunks = self._chunk_text(text, chunk_size)
        sent = 0
        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(0.1)  # small smoothing
            if await adapter.send_text(channel_id, chunk):
                sent += 1
        return sent

    @staticmethod
    def _chunk_text(text: str, chunk_size: int) -> list[str]:
        """Split `text` on paragraph boundaries so each chunk is
        ≤ chunk_size characters. A single paragraph longer than
        chunk_size emerges as its own oversize chunk (unchanged from
        prior behavior — callers were already tolerating that)."""
        if len(text) <= chunk_size:
            return [text]

        chunks: list[str] = []
        current = ""
        for paragraph in text.split("\n\n"):
            prospective = (current + "\n\n" + paragraph).strip()
            if len(prospective) > chunk_size and current:
                chunks.append(current)
                current = paragraph
            else:
                current = prospective
        if current:
            chunks.append(current)
        return chunks

    async def deliver_digest(
        self,
        adapter: "MessageSender",
        channel_id: str,
        text: str,
        *,
        chunk_size: int,
        prior_pin_id: str | None,
    ) -> tuple[int, str | None]:
        """Deliver a digest: send `text` (splitting on paragraphs if it
        exceeds `chunk_size`), pin the first chunk, and unpin the prior
        digest's pin when a new pin takes its place.

        Auto-pin is best-effort: if the adapter / channel doesn't allow
        pinning, the send still counts as a successful delivery. The
        old pin is left alone in that case so the channel retains
        *some* pinned digest rather than none — the caller will retry
        unpinning on the next delivery.

        Returns (chunks_sent, new_pin_id):
          - chunks_sent: total chunks that reached the channel. 0 means
            the whole delivery failed; caller must not mark delivered.
          - new_pin_id: platform message id of the freshly-pinned
            digest, or None if pinning was skipped / failed. Caller
            passes this straight to `mark_delivered(pinned_message_id=)`
            — None preserves the prior stored pin id so the next
            delivery can still try to unpin it.
        """
        if not text:
            return 0, None

        chunks = self._chunk_text(text, chunk_size)
        if not chunks:
            return 0, None

        sent_first, new_pin_id = await adapter.send_text_pinned(
            channel_id, chunks[0]
        )
        if not sent_first:
            return 0, None
        chunks_sent = 1

        for chunk in chunks[1:]:
            await asyncio.sleep(0.1)  # smoothing, mirrors _send_text_split
            if await adapter.send_text(channel_id, chunk):
                chunks_sent += 1

        # Only unpin the prior digest if a new pin actually took its
        # place — otherwise leaving the old pin preserves continuity
        # (better to see last week's pinned digest than nothing).
        if prior_pin_id and new_pin_id and prior_pin_id != new_pin_id:
            try:
                await adapter.unpin_message(channel_id, prior_pin_id)
            except Exception:
                logger.debug(
                    f"Unpin of prior digest {prior_pin_id} in "
                    f"{channel_id} raised; leaving stale pin in place"
                )

        return chunks_sent, new_pin_id

    async def run_digest_loop(
        self, check_interval_seconds: int | None = None
    ) -> None:
        """Periodically check for channels whose digest is due and deliver.

        Wakes every `digest_check_interval_minutes` by default. The loop
        itself is cheap; the is_due() check only fires heavy work (LLM +
        send) when a channel actually matches its schedule slot.
        """
        from newsflow.services.digest_service import DigestService, is_due
        from newsflow.services.summarization import get_summarizer

        interval = check_interval_seconds or (
            self.settings.digest_check_interval_minutes * 60
        )
        await self.wait_for_adapters(timeout=60.0)
        logger.info(f"Starting digest loop (check every {interval}s)")

        # Delay first check so startup noise settles.
        await asyncio.sleep(60)

        while True:
            try:
                await self._tick_digests()
            except Exception:
                logger.exception("Digest loop error")

            self._write_heartbeat("digest")
            await asyncio.sleep(interval)

    async def _tick_digests(self) -> None:
        from newsflow.services.digest_service import DigestService, is_due
        from newsflow.services.summarization import get_summarizer

        summarizer = get_summarizer()
        if summarizer is None:
            # No LLM configured — not an error, just nothing to do.
            return

        now = datetime.now(timezone.utc)

        session_factory = get_session_factory()
        async with session_factory() as session:
            from newsflow.repositories.digest_repository import (
                ChannelDigestRepository,
            )

            repo = ChannelDigestRepository(session)
            configs = await repo.list_enabled()

        for config in configs:
            if not is_due(config, now):
                continue
            adapter = self._adapters.get(config.platform)
            if adapter is None:
                logger.debug(
                    f"Digest due for {config.platform}/"
                    f"{config.platform_channel_id} but adapter not registered; "
                    f"deferring"
                )
                continue

            # Fresh session per channel so one failure doesn't poison others.
            try:
                async with session_factory() as session:
                    service = DigestService(session, summarizer)
                    service.repo = type(service.repo)(session)  # bind to this session
                    # Re-fetch config in this session's identity map to update it.
                    fresh_config = await service.repo.get(
                        config.platform, config.platform_channel_id
                    )
                    if fresh_config is None:
                        continue

                    result = await service.generate(fresh_config, now=now)
                    if result is None:
                        # No articles in window; still mark delivered so we
                        # don't keep re-firing this slot.
                        await service.repo.mark_delivered(fresh_config.id, now)
                        await session.commit()
                        continue

                    if not result.success:
                        logger.warning(
                            f"Digest generation failed for "
                            f"{config.platform}/{config.platform_channel_id}: "
                            f"{result.error}"
                        )
                        continue

                    digest_text = self.apply_digest_header(
                        result.text, config.platform
                    )

                    # Deliver. Discord text messages cap at ~2000 chars;
                    # Telegram at 4096. Use 1900 to be safe for both.
                    chunks_sent, new_pin_id = await self.deliver_digest(
                        adapter,
                        config.platform_channel_id,
                        digest_text,
                        chunk_size=1900,
                        prior_pin_id=fresh_config.last_pinned_message_id,
                    )
                    if chunks_sent == 0:
                        logger.warning(
                            f"Digest generated but send failed for "
                            f"{config.platform}/{config.platform_channel_id}"
                        )
                        continue

                    await service.repo.mark_delivered(
                        fresh_config.id, now, pinned_message_id=new_pin_id
                    )
                    await session.commit()
                    logger.info(
                        f"Delivered digest to {config.platform}/"
                        f"{config.platform_channel_id} ({chunks_sent} chunks)"
                    )
            except Exception:
                logger.exception(
                    f"Digest delivery failed for "
                    f"{config.platform}/{config.platform_channel_id}"
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
