"""
Message dispatcher service.

Handles fetching feeds and dispatching new entries to subscribed channels.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.adapters.base import (
    ChannelGoneError,
    ChannelMigratedError,
    Message,
    TopicGoneError,
)
from newsflow.config import get_settings
from newsflow.core.content_processor import (
    MAX_SUMMARY_LENGTH,
    clean_html,
    dedup_summary,
    get_source_name,
    truncate_text,
)
from newsflow.core.filter import FilterRule
from newsflow.core.languages import same_primary_language, text_clearly_in_language
from newsflow.core.message_template import render_template
from newsflow.models.base import get_session_factory
from newsflow.models.feed import FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.repositories.feed_repository import FeedRepository
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService
from newsflow.services.translation.factory import get_translation_service

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MessageSender(Protocol):
    """Protocol for message sending. Adapters supply send_message (for
    structured feed entries), send_text (for system notifications like
    feed-auto-disabled notices), send_digest_text + send_digest_text_pinned
    + unpin_message (for digest delivery — BaseAdapter defaults degrade to
    plain send_text without pin), and is_connected (for HEALTHCHECK)."""

    async def send_message(self, channel_id: str, message: Message) -> bool: ...

    async def send_text(self, channel_id: str, text: str) -> bool: ...

    async def send_text_pinned(self, channel_id: str, text: str) -> tuple[bool, str | None]: ...

    async def send_digest_text(self, channel_id: str, text: str) -> bool: ...

    async def send_digest_text_pinned(
        self, channel_id: str, text: str
    ) -> tuple[bool, str | None]: ...

    async def unpin_message(self, channel_id: str, message_id: str) -> bool: ...

    def is_connected(self) -> bool: ...

    # Per-message character budget for digest chunks.
    digest_chunk_size: int


@dataclass
class DispatchResult:
    """Result of a dispatch run."""

    feeds_fetched: int = 0
    new_entries: int = 0
    messages_sent: int = 0
    errors: int = 0


@dataclass
class DispatcherTotals:
    """Process-lifetime counters, accumulated per dispatch round and exposed
    by /metrics. Plain ints on purpose — the dispatcher stays free of any
    metrics-library dependency."""

    dispatch_rounds: int = 0
    feeds_fetched: int = 0
    new_entries: int = 0
    messages_sent: int = 0
    send_errors: int = 0


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
        # Serialises dispatch rounds: the loop and ingest-triggered rounds share this
        # path, and two interleaved rounds double-send.
        self._dispatch_mutex = asyncio.Lock()
        self.totals = DispatcherTotals()

    def spawn(self, coro: Any, *, name: str | None = None) -> asyncio.Task:
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

    def get_adapter(self, platform: str) -> MessageSender | None:
        """The registered adapter for a platform, or None before it connects."""
        return self._adapters.get(platform)

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
        except TimeoutError:
            logger.warning(f"Timed out waiting for adapters; missing: {self._expected_platforms}")
            return False

    async def dispatch_once(self) -> DispatchResult:
        """Run one dispatch cycle (fetch due feeds + deliver unsent entries).

        Rounds are serialised by a mutex: a concurrent caller (the loop vs an
        ingest-triggered round) waits for the current round instead of racing
        it into double-sends.
        """
        async with self._dispatch_mutex:
            result = await self._dispatch_once_inner()
        self.totals.dispatch_rounds += 1
        self.totals.feeds_fetched += result.feeds_fetched
        self.totals.new_entries += result.new_entries
        self.totals.messages_sent += result.messages_sent
        self.totals.send_errors += result.errors
        return result

    async def _dispatch_once_inner(self) -> DispatchResult:
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

                # Commit the fetch writes BEFORE the subscription loop: on SQLite they hold
                # the single-writer lock, which stalls the webhook breaker (own session) for
                # the full busy-timeout on every failing send.
                await session.commit()

                # Dispatch to every subscription each cycle, not only when this cycle produced
                # entries: a subscription can hold a backlog whose earlier send failed
                # transiently, and gating on new_entries strands it past the age cutoff.
                sub_repo = SubscriptionRepository(session)
                subscriptions = await sub_repo.get_all_active_subscriptions()
                # Iterate a plain-id snapshot and re-fetch at the top: a failed per-sub commit
                # rolls back and expires EVERY ORM instance in the session, so a cached
                # object would raise MissingGreenlet and abort the rest of the round.
                sub_ids = [s.id for s in subscriptions]
                # Channels already found gone this cycle. The bulk UPDATE does not sync
                # identity-mapped instances, so the re-fetch still sees is_active=True.
                dead_channels: set[tuple[str, str]] = set()
                for sub_id in sub_ids:
                    sub = await sub_repo.get_subscription_by_id(sub_id)
                    if sub is None or not sub.is_active:
                        # Deleted or deactivated since the snapshot (only
                        # visible here after a rollback refreshed the row).
                        continue
                    if (sub.platform, sub.platform_channel_id) in dead_channels:
                        continue
                    sent = await self._dispatch_to_subscription(
                        session,
                        sub,
                        sub_repo,
                        dead_channels=dead_channels,
                    )
                    result.messages_sent += sent
                    # Commit after each subscription, never once per round: messages went out when
                    # the adapter returned, so a late failure would roll back the whole round's
                    # sent-marks and re-push everything. expire_on_commit=False keeps objects usable.
                    try:
                        await session.commit()
                    except Exception:
                        logger.exception(
                            f"Commit failed after subscription {sub.id}; "
                            f"its sent-marks may replay next cycle"
                        )
                        await session.rollback()

                # Final commit covers rounds with zero subscriptions: feed metadata (etag /
                # backoff / error_count) must persist or the ETag cache, backoff and the
                # 10-errors auto-deactivate are all silently defeated.
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
        *,
        dead_channels: set[tuple[str, str]] | None = None,
        bypass_silent: bool = False,
    ) -> int:
        """
        Dispatch new entries to a single subscription.

        Args:
            dead_channels: optional set the caller uses to skip remaining
                subs for a channel once any sub on it has surfaced as
                gone. Updated in-place when this call hits ChannelGoneError.
                None for one-shot callers (e.g. the preview path) where
                the cycle-wide skip semantics don't apply.
            bypass_silent: when True, ignore the subscription's `silent`
                flag and deliver normally. Used by the post-subscribe
                preview path so the user always sees one confirmation
                article, even on silent subscriptions.

        Returns:
            Number of messages sent
        """
        # Get adapter for platform
        adapter = self._adapters.get(subscription.platform)
        if not adapter:
            logger.warning(f"No adapter for platform: {subscription.platform}")
            return 0

        # Get unsent entries
        entries = await sub_repo.get_unsent_entries_for_subscription(subscription.id, limit=10)

        if not entries:
            return 0

        # Decode the subscription's keyword filter once for this batch.
        # Empty rule matches everything (no-op filter).
        filter_rule = FilterRule.from_json(subscription.filter_rule)

        sent_count = 0
        for entry in entries:
            try:
                # Filter before the expensive translate/send path; filtered entries are marked
                # processed. Match on CLEANED text — raw markup fires exclude words on URLs
                # and hides the article body from filters entirely.
                if not filter_rule.is_empty():
                    summary_text, _ = clean_html(entry.summary or "")
                    content_text, _ = clean_html(entry.content or "")
                    haystack = f"{entry.title} {summary_text} {content_text}"
                    if not filter_rule.matches(haystack):
                        await sub_repo.mark_entry_sent(
                            subscription.id,
                            entry.feed_id,
                            entry.guid,
                            was_filtered=True,
                        )
                        logger.debug(
                            f"Entry {entry.id} filtered out for "
                            f"{subscription.platform}/{subscription.platform_channel_id}"
                        )
                        continue

                # Silent mode: no instant push, but mark sent (was_filtered=False) so digest
                # picks it up via SentEntry. Translation skipped — digest uses the original.
                # bypass_silent=True is the preview path (one confirmation article).
                if subscription.silent and not bypass_silent:
                    await sub_repo.mark_entry_sent(
                        subscription.id,
                        entry.feed_id,
                        entry.guid,
                        was_filtered=False,
                    )
                    logger.debug(
                        f"Entry {entry.id} silenced for "
                        f"{subscription.platform}/{subscription.platform_channel_id} "
                        f"(digest will pick it up)"
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
                    await sub_repo.mark_entry_sent(subscription.id, entry.feed_id, entry.guid)
                    sent_count += 1
                    logger.debug(
                        f"Sent entry {entry.id} to {subscription.platform}/{subscription.platform_channel_id}"
                    )
                else:
                    logger.warning(
                        f"Failed to send entry {entry.id} to {subscription.platform}/{subscription.platform_channel_id}"
                    )

                # Smoothing pause only. Real rate limiting lives in the libraries
                # (discord.py buckets, Telegram AIORateLimiter).
                await asyncio.sleep(0.1)

            except TopicGoneError as e:
                # Forum topic deleted, chat alive: clear the thread so delivery falls back to
                # the default view. This entry stays unsent; the rest of the batch already
                # builds against the cleared value. Idempotent — a rollback just re-heals.
                subscription.message_thread_id = None
                logger.warning(
                    f"Topic {e.thread_id} in {subscription.platform}/"
                    f"{subscription.platform_channel_id} is gone; subscription "
                    f"{subscription.id} repointed to the default topic"
                )
                continue

            except ChannelMigratedError as e:
                # Telegram supergroup upgrade: same channel, new chat id, old id rejects every
                # send. Repoint all subs + digest config; unsent entries deliver there next
                # cycle. The outer dispatch_once commit persists it.
                from newsflow.repositories.digest_repository import (
                    ChannelDigestRepository,
                )

                # Capture before migrate_channel: it rewrites the identity-
                # mapped `subscription` object in place, so reading the
                # attribute afterwards would yield the NEW id.
                platform = subscription.platform
                old_channel_id = subscription.platform_channel_id

                moved = await sub_repo.migrate_channel(platform, old_channel_id, e.new_channel_id)
                digests_moved = await ChannelDigestRepository(session).migrate_channel(
                    platform, old_channel_id, e.new_channel_id
                )
                if dead_channels is not None:
                    # Remaining cached subs in this cycle still carry the
                    # old id — skip them; next cycle reads the migrated
                    # rows and delivers everything to the new id.
                    dead_channels.add((platform, old_channel_id))
                logger.warning(
                    f"Channel {platform}/{old_channel_id} migrated to "
                    f"{e.new_channel_id}; repointed {moved} subscription(s) "
                    f"and {digests_moved} digest config(s)"
                )
                return sent_count

            except ChannelGoneError as e:
                # Channel permanently unreachable: deactivate every active subscription for it
                # (a channel usually has many feeds) and disable any digest config.
                # Idempotent — the WHERE is_active=True clause no-ops on repeat.
                from newsflow.repositories.digest_repository import (
                    ChannelDigestRepository,
                )

                subs_flipped = await sub_repo.deactivate_channel(
                    subscription.platform, subscription.platform_channel_id
                )
                digest_repo = ChannelDigestRepository(session)
                digests_flipped = await digest_repo.disable_for_channel(
                    subscription.platform, subscription.platform_channel_id
                )
                # Tell dispatch_once to skip remaining cached subs on this channel, which is
                # what makes the warning below fire exactly once per channel per cycle.
                # The rowcount check stays as defence for callers that pass no set.
                if dead_channels is not None:
                    dead_channels.add((subscription.platform, subscription.platform_channel_id))
                if subs_flipped or digests_flipped:
                    logger.warning(
                        f"Channel {subscription.platform}/"
                        f"{subscription.platform_channel_id} is gone "
                        f"({e.reason or 'unreachable'}); deactivated "
                        f"{subs_flipped} subscription(s) and "
                        f"{digests_flipped} digest config(s)"
                    )
                # Stop processing remaining entries for this sub — the
                # channel is dead, further attempts would just re-raise.
                return sent_count

            except SQLAlchemyError:
                # A failed mark/flush has poisoned the transaction: every later mark in this
                # batch fails while its message was already pushed — a guaranteed duplicate
                # next cycle. Stop the batch; the round continues with the next subscription.
                logger.exception(
                    f"DB error marking entry {entry.id} for subscription "
                    f"{subscription.id}; aborting this subscription's batch"
                )
                break

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
        if entry.translation_language == target_language and entry.title_translated:
            return entry.title_translated, entry.summary_translated

        # Same-language short-circuit #1 (free, script-based). Only for script-unique
        # languages (zh/ja/ko) and never across the simplified<->traditional boundary.
        combined = f"{entry.title or ''} {plain_summary}".strip()
        if combined and text_clearly_in_language(combined, target_language):
            logger.debug(f"Entry {entry.id} already in {target_language}; skipping translation")
            return None, None

        # Get translation service
        translation_service = get_translation_service()
        if not translation_service:
            return None, None

        title_translated = None
        summary_translated = None

        try:
            # Translate title
            if entry.title:
                result = await translation_service.translate(entry.title, target_language)
                if result.success:
                    # Same-language short-circuit #2 (provider-informed). Drop the provider output
                    # rather than adopt a "polished" identity translation, and cache the originals.
                    # zh is excluded: detectors report bare "ZH" and cannot see simplified/traditional.
                    if same_primary_language(
                        result.source_language, target_language
                    ) and not target_language.lower().startswith("zh"):
                        feed_repo = FeedRepository(session)
                        await feed_repo.update_entry_translation(
                            entry_id=entry.id,
                            title_translated=entry.title,
                            summary_translated=plain_summary,
                            language=target_language,
                        )
                        logger.debug(
                            f"Entry {entry.id} detected as {result.source_language} == "
                            f"{target_language}; skipping translation"
                        )
                        return None, None
                    title_translated = result.translated_text

            # Translate summary (cap length to keep token usage bounded)
            if plain_summary:
                summary_text = plain_summary[:1000]
                result = await translation_service.translate(summary_text, target_language)
                if result.success:
                    summary_translated = result.translated_text

            # Cache in the DB only when every attempted field came back. A partial result
            # would freeze the gap in: the early-cache check would short-circuit future
            # dispatches and the missing half would never be retried.
            title_ok = title_translated is not None or not entry.title
            summary_ok = summary_translated is not None or not plain_summary
            if title_ok and summary_ok and (title_translated or summary_translated):
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

        # Feeds embed raw HTML in summary/description and the platforms do not render
        # it. Strip here; prefer `content` (fuller) over `summary`.
        raw_body = entry.content or entry.summary or ""
        plain_summary, _images = clean_html(raw_body)
        plain_summary = truncate_text(plain_summary, MAX_SUMMARY_LENGTH)
        # Drop summaries that merely echo the title (rules in content_processor.
        # dedup_summary). Dedup runs BEFORE translation so the redundant text also
        # costs no API call.
        plain_summary = dedup_summary(entry.title, plain_summary)

        # Translated fields start empty. The FeedEntry cache is shared by every
        # subscription to this feed, so seeding here would hand a channel with
        # translation off — or another target — whatever some other channel cached.
        title_translated: str | None = None
        summary_translated: str | None = None

        # Translate if enabled for this subscription. Pass the cleaned
        # summary so we don't spend tokens translating <p> tags or get back
        # a translation that still contains HTML.
        if subscription.translate:
            title_translated, summary_translated = await self._translate_entry(
                entry, subscription.target_language, session, plain_summary
            )

        # Custom template renders from PRE-trim values so {summary}/{image_url} always
        # resolve; show_summary is deliberately ignored. Render problems fall back to
        # the default layout — a broken template must never lose an article.
        template_text: str | None = None
        if subscription.message_template:
            pretrim = Message(
                title=entry.title,
                summary=plain_summary,
                link=entry.link,
                source=source,
                published_at=entry.published_at,
                image_url=entry.image_url,
                title_translated=title_translated,
                summary_translated=summary_translated,
                mention=subscription.mention,
            )
            try:
                template_text = (
                    render_template(subscription.message_template, pretrim.to_template_values())
                    or None
                )
            except Exception:
                logger.exception(f"Template render failed for subscription {subscription.id}")
                template_text = None

        # Per-subscription display controls, applied at message build AFTER translation
        # so a hidden summary still warms the shared translation cache.
        if subscription.show_summary is False:
            plain_summary = ""
            summary_translated = None
        show_image = subscription.show_image is not False
        image_url = entry.image_url if show_image else None

        return Message(
            title=entry.title,
            summary=plain_summary,
            link=entry.link,
            source=source,
            published_at=entry.published_at,
            image_url=image_url,
            title_translated=title_translated,
            summary_translated=summary_translated,
            template_text=template_text,
            mention=subscription.mention,
            thread_id=subscription.message_thread_id,
            show_image=show_image,
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
                    logger.exception(f"adapter.is_connected() raised for {platform}")
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

            # bypass_silent: a freshly-subscribed silent channel should
            # still see one confirmation article so the user knows the
            # subscription took. Subsequent dispatch cycles honor silent.
            sent = await self._dispatch_to_subscription(session, sub, sub_repo, bypass_silent=True)
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
                subs = await sub_repo.get_feed_subscriptions(feed_id, include_inactive=True)

            if not subs:
                return

            name = feed_title or feed_url

            # Recovery instructions differ per platform — quoting a Discord
            # command shape at a Telegram user sends them down a dead end.
            def _text_for(platform: str) -> str:
                if platform == "telegram":
                    hint = (
                        "Use /resume <url> once the source is working again, "
                        "or /remove <url> to clean up."
                    )
                elif platform == "webhook":
                    hint = (
                        "Feeds declared in YAML are re-enabled automatically "
                        "on the next bot restart; remove it from the file to "
                        "clean up."
                    )
                else:
                    hint = (
                        "Use /feed resume <url> once the source is working "
                        "again, or /feed remove <url> to clean up."
                    )
                return (
                    f'⚠️ The RSS feed "{name}" has been auto-disabled after '
                    f"10 consecutive fetch errors. {hint}\nURL: {feed_url}"
                )

            for sub in subs:
                adapter = self._adapters.get(sub.platform)
                if adapter is None:
                    continue
                try:
                    await adapter.send_text(sub.platform_channel_id, _text_for(sub.platform))
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
            logger.exception(f"Preview dispatch failed for subscription {subscription_id}")

    # Zero-width space — invisible in rendered text, but it breaks Discord's
    # mass-mention token parse, turning model-emitted @everyone/@here inert.
    _ZWSP = "\u200b"

    def apply_digest_header(self, text: str, platform: str) -> str:
        """Prepend a visible header + platform-appropriate mention to
        digest text when DIGEST_MENTION_ON_DELIVERY is enabled.

        Shared between the scheduled digest path (`_tick_digests`) and
        the manual `/digest now` handlers in both Discord and Telegram
        adapters, so either trigger produces the same delivery shape.
        Without this shim the 3 paths had drifted — mention only fired
        on scheduled runs, not on manual test runs.

        Discord gets `@here`, and the Discord adapter sends digest text
        with AllowedMentions(everyone=True) so it actually pings —
        overriding the client-wide AllowedMentions.none() baseline that
        otherwise neuters every mention in bot content. Because the
        digest body is LLM-generated, mass-mention tokens inside it are
        neutralized (zero-width insert) BEFORE the header is prepended:
        only the code-added header can ever ping. (The bot still needs
        the "Mention Everyone" channel permission — Discord requires
        both the permission and the allowance.) Telegram / webhook just
        get the visible header — no mention token since those platforms'
        notification model differs.
        """
        if not self.settings.digest_mention_on_delivery:
            return text
        if platform == "discord":
            body = text.replace("@everyone", f"@{self._ZWSP}everyone").replace(
                "@here", f"@{self._ZWSP}here"
            )
            return "@here 📰 **Digest**\n\n" + body
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
        """Split `text` so each chunk is ≤ chunk_size characters.

        Tries paragraph (`\\n\\n`) boundaries first, then line (`\\n`)
        boundaries for any paragraph that still exceeds chunk_size, then
        a hard character-count slice as last resort. The hard-slice
        fallback exists because Discord rejects content > 4000 chars
        (error 50035) — a long LLM-generated paragraph without blank
        lines would otherwise fail the whole digest delivery.
        """
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

        # Second pass: any chunk that's still > chunk_size (single
        # paragraph without blank lines) gets broken further.
        final: list[str] = []
        for chunk in chunks:
            if len(chunk) <= chunk_size:
                final.append(chunk)
            else:
                final.extend(Dispatcher._hard_split(chunk, chunk_size))
        return final

    @staticmethod
    def _hard_split(text: str, chunk_size: int) -> list[str]:
        """Break an oversized chunk on single-newline boundaries, then
        by hard character count. Only called when paragraph splitting
        already failed to get below chunk_size."""
        if len(text) <= chunk_size:
            return [text]

        pieces: list[str] = []
        current = ""
        for line in text.split("\n"):
            prospective = (current + "\n" + line) if current else line
            if len(prospective) > chunk_size and current:
                pieces.append(current)
                current = line
            else:
                current = prospective
        if current:
            pieces.append(current)

        out: list[str] = []
        for piece in pieces:
            if len(piece) <= chunk_size:
                out.append(piece)
            else:
                # Last resort: slice by characters. Rare — only when a
                # single line (no \n, no \n\n) exceeds chunk_size.
                for i in range(0, len(piece), chunk_size):
                    out.append(piece[i : i + chunk_size])
        return out

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

        # Digest-specific send methods: platforms that can't render the
        # digest's Markdown natively re-render per chunk (Telegram →
        # HTML + previews off); everyone else falls through to send_text.
        sent_first, new_pin_id = await adapter.send_digest_text_pinned(channel_id, chunks[0])
        if not sent_first:
            return 0, None
        chunks_sent = 1

        for chunk in chunks[1:]:
            await asyncio.sleep(0.1)  # smoothing, mirrors _send_text_split
            if await adapter.send_digest_text(channel_id, chunk):
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

    async def run_digest_loop(self, check_interval_seconds: int | None = None) -> None:
        """Periodically check for channels whose digest is due and deliver.

        Wakes every `digest_check_interval_minutes` by default. The loop
        itself is cheap; the is_due() check only fires heavy work (LLM +
        send) when a channel actually matches its schedule slot.
        """
        interval = check_interval_seconds or (self.settings.digest_check_interval_minutes * 60)
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

        now = datetime.now(UTC)

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
            channel = f"{config.platform}/{config.platform_channel_id}"

            try:
                outcome = await DigestService.run_now(
                    self,
                    config.platform,
                    config.platform_channel_id,
                    summarizer,
                    now,
                    # A scheduled run consumes its slot even on an empty window,
                    # or is_due re-fires it on every tick until the hour passes.
                    mark_empty_delivered=True,
                )
                if outcome.status == "no_adapter":
                    logger.debug(f"Digest due for {channel} but adapter not registered; deferring")
                elif outcome.status == "generation_failed":
                    logger.warning(f"Digest generation failed for {channel}: {outcome.error}")
                elif outcome.status == "delivery_failed":
                    logger.warning(f"Digest generated but send failed for {channel}")
                elif outcome.status == "delivered":
                    logger.info(f"Delivered digest to {channel} ({outcome.chunks} chunks)")
            except ChannelMigratedError as e:
                # Same channel, new chat id (supergroup upgrade). Repoint in a fresh session;
                # the digest was not marked delivered, so the next tick redelivers.
                from newsflow.repositories.subscription_repository import (
                    SubscriptionRepository,
                )

                # Capture before migrating — migrate_channel may rewrite
                # the identity-mapped `config` object in place.
                platform = config.platform
                old_channel_id = config.platform_channel_id

                try:
                    async with session_factory() as mig_session:
                        moved = await SubscriptionRepository(mig_session).migrate_channel(
                            platform, old_channel_id, e.new_channel_id
                        )
                        digests_moved = await ChannelDigestRepository(mig_session).migrate_channel(
                            platform, old_channel_id, e.new_channel_id
                        )
                        await mig_session.commit()
                    logger.warning(
                        f"Digest channel {platform}/{old_channel_id} "
                        f"migrated to {e.new_channel_id}; repointed {moved} "
                        f"subscription(s) and {digests_moved} digest "
                        f"config(s)"
                    )
                except Exception:
                    logger.exception(f"Failed to migrate channel {platform}/{old_channel_id}")
            except ChannelGoneError as e:
                # Digest target gone: disable the digest config AND any remaining active subs
                # on this channel — this tick may run before the next feed dispatch visits
                # them. Fresh session per channel keeps commits independent.
                from newsflow.repositories.subscription_repository import (
                    SubscriptionRepository,
                )

                try:
                    async with session_factory() as cleanup_session:
                        sub_repo = SubscriptionRepository(cleanup_session)
                        subs_flipped = await sub_repo.deactivate_channel(
                            config.platform, config.platform_channel_id
                        )
                        cleanup_digest_repo = ChannelDigestRepository(cleanup_session)
                        digests_flipped = await cleanup_digest_repo.disable_for_channel(
                            config.platform, config.platform_channel_id
                        )
                        await cleanup_session.commit()
                    logger.warning(
                        f"Digest channel {config.platform}/"
                        f"{config.platform_channel_id} is gone "
                        f"({e.reason or 'unreachable'}); deactivated "
                        f"{subs_flipped} subscription(s) and "
                        f"{digests_flipped} digest config(s)"
                    )
                except Exception:
                    logger.exception(
                        f"Failed to clean up gone channel "
                        f"{config.platform}/{config.platform_channel_id}"
                    )
            except Exception:
                logger.exception(
                    f"Digest delivery failed for {config.platform}/{config.platform_channel_id}"
                )

    async def run_cleanup_loop(self, heartbeat_tick_seconds: int = 300) -> None:
        """Periodically delete old feed entries and sent-entry records.

        Cleanup work runs every `settings.cleanup_interval_hours` (default
        24h). FeedEntry rows older than `entry_retention_days` are
        deleted; SentEntry rows older than `sent_entry_retention_days`
        are deleted (much longer because it's the dedupe signal).

        Heartbeat is touched every `heartbeat_tick_seconds` (default 5
        min), independent of the cleanup cadence. The Dockerfile
        HEALTHCHECK fails any heartbeat file older than 120 min, and
        the natural 24h cleanup interval would otherwise leave this
        heartbeat stale and the container marked unhealthy. Splitting
        the two cadences keeps "loop alive" and "do work" decoupled.
        """
        interval_seconds = self.settings.cleanup_interval_hours * 3600
        entry_retention_days = self.settings.entry_retention_days
        sent_retention_days = self.settings.sent_entry_retention_days

        logger.info(
            f"Starting cleanup loop (every {self.settings.cleanup_interval_hours}h, "
            f"entry retention {entry_retention_days}d, "
            f"sent-entry retention {sent_retention_days}d, "
            f"heartbeat tick {heartbeat_tick_seconds}s)"
        )

        # Delay first run so startup logs stay clean and DB is definitely up.
        await asyncio.sleep(60)

        # Monotonic clock — system time changes (NTP) must not skew our
        # cleanup cadence.
        loop = asyncio.get_running_loop()
        next_cleanup_at = loop.time()  # first tick runs cleanup immediately

        while True:
            if loop.time() >= next_cleanup_at:
                try:
                    session_factory = get_session_factory()
                    async with session_factory() as session:
                        feed_repo = FeedRepository(session)
                        sub_repo = SubscriptionRepository(session)

                        entries_deleted = await feed_repo.cleanup_old_entries(entry_retention_days)
                        sent_deleted = await sub_repo.cleanup_old_sent_entries(sent_retention_days)
                        await session.commit()

                        logger.info(
                            f"Cleanup: deleted {entries_deleted} old entries, "
                            f"{sent_deleted} old sent records"
                        )
                except Exception:
                    logger.exception("Cleanup loop error")
                # Always advance the next-run pointer, even on error —
                # avoids a tight retry loop under persistent lock pressure.
                next_cleanup_at = loop.time() + interval_seconds

            # Touch heartbeat every tick so HEALTHCHECK doesn't flag this
            # task as stale during the long sleep between cleanup runs.
            self._write_heartbeat("cleanup")
            await asyncio.sleep(heartbeat_tick_seconds)

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
