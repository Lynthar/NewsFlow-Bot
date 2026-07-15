"""
Feed service - Business logic for feed management.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.config import get_settings
from newsflow.core.feed_fetcher import FetchResult, get_fetcher
from newsflow.core.source_fetcher import (
    PUSH_SOURCE_TYPES,
    SourceRequest,
    get_source_fetcher,
)
from newsflow.core.source_shortcuts import expand_source_shortcut
from newsflow.models.feed import Feed, FeedEntry
from newsflow.repositories.feed_repository import FeedRepository

logger = logging.getLogger(__name__)


class SourceFeedConflictError(ValueError):
    """Raised by upsert_source_feed when a declared non-RSS source URL collides
    with an existing plain-RSS feed. sources.yaml only owns non-RSS feeds, so
    rather than silently convert (and later delete) a user's RSS feed, the sync
    skips the colliding source and logs it."""


@dataclass
class AddFeedResult:
    """Result of adding a feed."""

    success: bool
    feed: Feed | None = None
    message: str = ""
    entry_count: int = 0


@dataclass
class FetchFeedResult:
    """Result of fetching a feed."""

    success: bool
    feed: Feed | None = None
    new_entries: list[FeedEntry] = field(default_factory=list)
    message: str = ""


class FeedService:
    """
    Service for feed management operations.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = FeedRepository(session)
        self.fetcher = get_fetcher()
        self.settings = get_settings()

    @property
    def _backoff_base_seconds(self) -> int:
        """Base unit for exponential backoff on fetch errors: one full
        fetch interval. Doubles each error (capped in Feed.mark_error)."""
        return self.settings.fetch_interval_minutes * 60

    async def add_feed(self, url: str) -> AddFeedResult:
        """
        Add a new feed by URL.

        This will:
        1. Validate the URL
        2. Fetch and parse the feed
        3. Create feed record
        4. Store initial entries

        Args:
            url: The RSS feed URL

        Returns:
            AddFeedResult with success status and feed object
        """
        # Expand `gh:owner/repo` / `gnews:keyword` shortcuts into the real feed
        # URL before anything else, so dedupe and storage key on the resolved
        # URL. A normal URL is returned unchanged.
        url = expand_source_shortcut(url)

        # Check if feed already exists
        existing = await self.repo.get_feed_by_url(url)
        if existing:
            # Re-adding is the natural "I want this working again" signal:
            # an auto-disabled feed (10 straight errors) has no other
            # user-reachable revival path — fetch skips inactive feeds, so
            # its error state can never clear on its own.
            if not existing.is_active:
                existing.reactivate()
                logger.info(f"Reactivated auto-disabled feed on re-add: {url}")
            return AddFeedResult(
                success=True,
                feed=existing,
                message="Feed already exists",
            )

        # Fetch and validate feed
        result = await self.fetcher.fetch_feed(url)

        # If `url` was an HTML page that advertises a feed, fetch_feed returns
        # the candidate(s) in discovered_feeds. Resolve to the first and retry
        # once, re-checking dedupe against the resolved URL.
        if not result.success and result.discovered_feeds:
            discovered = result.discovered_feeds[0]
            existing = await self.repo.get_feed_by_url(discovered)
            if existing:
                if not existing.is_active:
                    existing.reactivate()
                    logger.info(f"Reactivated auto-disabled feed on re-add: {discovered}")
                return AddFeedResult(
                    success=True,
                    feed=existing,
                    message="Feed already exists",
                )
            retry = await self.fetcher.fetch_feed(discovered)
            if retry.success and retry.entries:
                logger.info(f"add_feed: resolved {url} to advertised feed {discovered}")
                url, result = discovered, retry

        if not result.success:
            return AddFeedResult(
                success=False,
                message=f"Failed to fetch feed: {result.error}",
            )

        if not result.entries:
            return AddFeedResult(
                success=False,
                message="Feed has no entries",
            )

        # Create feed record. Guard against a race where two concurrent
        # subscribers pass the get_feed_by_url check at the same time and
        # both try to INSERT — SQLite's unique constraint on Feed.url makes
        # the second INSERT raise IntegrityError instead of silently
        # succeeding, and we want to surface that as "reuse the existing"
        # rather than a SQL stack trace to the user.
        try:
            feed = await self.repo.create_feed(
                url=url,
                title=result.feed_title,
                description=result.feed_description,
                site_url=result.feed_link,
            )

            # Update with cache headers
            await self.repo.update_feed_metadata(
                feed_id=feed.id,
                etag=result.etag,
                last_modified=result.last_modified,
            )

            # Store entries
            entries = await self.repo.create_entries_bulk(feed.id, result.entries)
        except IntegrityError:
            await self.session.rollback()
            existing = await self.repo.get_feed_by_url(url)
            if existing is None:
                return AddFeedResult(
                    success=False,
                    message="Concurrent add race — please retry",
                )
            logger.info(f"add_feed: concurrent race on {url}, reusing winner")
            return AddFeedResult(
                success=True,
                feed=existing,
                message="Feed already exists",
            )

        logger.info(f"Added feed: {url} with {len(entries)} entries")

        return AddFeedResult(
            success=True,
            feed=feed,
            message=f"Feed added with {len(entries)} entries",
            entry_count=len(entries),
        )

    async def upsert_source_feed(self, url: str, source_type: str, config: dict | None) -> Feed:
        """Create or update a non-RSS source feed (json_api, email_imap, …).

        Unlike add_feed, this does NOT fetch over HTTP — the registered
        SourceFetcher pulls entries on the next dispatch cycle. Used by the
        declarative sources.yaml sync.

        Raises SourceFeedConflictError if `url` already exists as a plain-RSS
        feed (interactively added via /feed add). Adopting it would convert it
        to a non-RSS type, overwrite its config, and make it eligible for
        deletion when the source later leaves the file — silently hijacking a
        user's feed. sources.yaml's ownership boundary is non-RSS feeds only.
        """
        existing = await self.repo.get_feed_by_url(url)
        if existing is not None:
            if (existing.source_type or "rss") == "rss":
                raise SourceFeedConflictError(
                    f"sources.yaml URL {url!r} already exists as an "
                    f"interactively-added RSS feed; refusing to convert it to "
                    f"{source_type!r}"
                )
            existing.source_type = source_type
            existing.config = config
            if not existing.is_active:
                # Full revival, not just the flag: with a stale error_count
                # of 10, the very next fetch error would disable it again.
                existing.reactivate()
            return existing
        return await self.repo.create_feed(url=url, source_type=source_type, config=config)

    async def test_feed(self, url: str) -> FetchResult:
        """
        Test if a feed URL is valid.

        Args:
            url: The RSS feed URL

        Returns:
            FetchResult with success status and entries
        """
        return await self.fetcher.fetch_feed(expand_source_shortcut(url))

    async def _apply_fetch_result(self, feed: Feed, result: FetchResult) -> FetchFeedResult:
        """Write a FetchResult to the DB. No network I/O — safe to call
        sequentially over a batch of already-fetched results."""
        if not result.success:
            was_active = feed.is_active
            await self.repo.mark_feed_error(
                feed.id, result.error, base_delay_seconds=self._backoff_base_seconds
            )
            # Feed.mark_error mutates the same ORM instance via the identity
            # map, so feed.is_active now reflects the post-update state.
            if was_active and not feed.is_active:
                # Transitioned this call — notify subscribers in a separate
                # session (theirs; ours isn't committed yet). Pass identity
                # by value so the notify task doesn't need to re-read.
                # spawn() holds a strong ref so the task isn't GC'd mid-run.
                from newsflow.services.dispatcher import get_dispatcher

                get_dispatcher().spawn(
                    get_dispatcher().notify_feed_deactivated(feed.id, feed.url, feed.title),
                    name=f"notify_feed_deactivated:{feed.id}",
                )
            return FetchFeedResult(
                success=False,
                feed=feed,
                message=f"Fetch error: {result.error}",
            )

        if result.not_modified:
            await self.repo.update_feed_metadata(feed.id)
            return FetchFeedResult(success=True, feed=feed, message="Not modified")

        await self.repo.update_feed_metadata(
            feed_id=feed.id,
            title=result.feed_title,
            description=result.feed_description,
            etag=result.etag,
            last_modified=result.last_modified,
        )

        if result.entries:
            new_entries = await self.repo.create_entries_bulk(feed.id, result.entries)
            logger.info(f"Feed {feed.url}: {len(new_entries)} new entries")
            return FetchFeedResult(
                success=True,
                feed=feed,
                new_entries=new_entries,
                message=f"{len(new_entries)} new entries",
            )

        return FetchFeedResult(success=True, feed=feed, message="No new entries")

    async def fetch_and_store(self, feed: Feed) -> FetchFeedResult:
        """
        Fetch a single feed and store new entries. Used by single-feed
        callers (e.g. the API `/refresh` endpoint).

        Routes by source_type exactly like fetch_all_feeds: RSS uses the HTTP
        fetcher, other pull types (json_api, email_imap, …) go through their
        registered SourceFetcher, and push types (webhook_inbound) have nothing
        to poll — entries arrive via /api/ingest — so they report a no-op
        success rather than being force-fetched as RSS (which would 404 / fail).
        """
        source_type = feed.source_type or "rss"
        if source_type in PUSH_SOURCE_TYPES:
            return FetchFeedResult(
                success=True,
                feed=feed,
                message="Push source — entries arrive via the ingest API, " "nothing to poll",
            )
        try:
            if source_type == "rss":
                result = await self.fetcher.fetch_feed(
                    url=feed.url,
                    etag=feed.etag,
                    last_modified=feed.last_modified,
                )
            else:
                result = await self._fetch_non_rss_source(feed)
            return await self._apply_fetch_result(feed, result)
        except Exception as e:
            logger.exception(f"Error fetching feed {feed.url}: {e}")
            await self.repo.mark_feed_error(
                feed.id, str(e), base_delay_seconds=self._backoff_base_seconds
            )
            return FetchFeedResult(success=False, feed=feed, message=f"Error: {str(e)}")

    async def fetch_all_feeds(self) -> list[FetchFeedResult]:
        """
        Fetch all active feeds concurrently, then apply DB updates serially.

        Concurrency is bounded by FeedFetcher's internal semaphore
        (max_concurrent=10). DB writes stay serial because a single
        AsyncSession is not safe to share across concurrent awaits.
        """
        feeds = await self.repo.get_feeds_due_for_fetch()
        if not feeds:
            return []

        # RSS keeps its optimized concurrent batch path. Other source types
        # (json_api, email_imap, …) are fetched via their registered
        # SourceFetcher; each yields the same FetchResult shape so everything
        # downstream is identical. With only RSS feeds present (the default),
        # this is the same single fetch_multiple call as before.
        rss_feeds = [f for f in feeds if (f.source_type or "rss") == "rss"]
        # Push sources (webhook_inbound) receive entries via the API, not by
        # polling — no fetcher, so leave them out of the fetch entirely.
        other_feeds = [
            f for f in feeds if f.source_type != "rss" and f.source_type not in PUSH_SOURCE_TYPES
        ]

        results_by_id: dict[int, FetchResult] = {}
        if rss_feeds:
            rss_results = await self.fetcher.fetch_multiple(
                [
                    {
                        "url": f.url,
                        "etag": f.etag,
                        "last_modified": f.last_modified,
                    }
                    for f in rss_feeds
                ]
            )
            for f, fr in zip(rss_feeds, rss_results):
                results_by_id[f.id] = fr
        for f in other_feeds:
            results_by_id[f.id] = await self._fetch_non_rss_source(f)

        results: list[FetchFeedResult] = []
        for feed in feeds:  # apply in the original feed order
            if feed.id not in results_by_id:
                continue  # push source — not polled; entries arrive via the API
            fr = results_by_id[feed.id]
            try:
                results.append(await self._apply_fetch_result(feed, fr))
            except Exception as e:
                logger.exception(f"Error applying fetch result for {feed.url}: {e}")
                await self.repo.mark_feed_error(
                    feed.id,
                    str(e),
                    base_delay_seconds=self._backoff_base_seconds,
                )
                results.append(
                    FetchFeedResult(
                        success=False,
                        feed=feed,
                        message=f"Error: {str(e)}",
                    )
                )

        return results

    async def _fetch_non_rss_source(self, feed: Feed) -> FetchResult:
        """Fetch one non-RSS feed via its registered SourceFetcher, converting
        any failure into a FetchResult so a single bad source (or an
        unregistered type) can't abort the whole dispatch cycle."""
        fetcher = get_source_fetcher(feed.source_type)
        if fetcher is None:
            return FetchResult(
                url=feed.url,
                success=False,
                entries=[],
                error=f"No fetcher registered for source_type {feed.source_type!r}",
            )
        try:
            return await fetcher.fetch(
                SourceRequest(
                    url=feed.url,
                    etag=feed.etag,
                    last_modified=feed.last_modified,
                    config=feed.config,
                )
            )
        except Exception as e:
            logger.exception(f"Source fetch failed for {feed.url}: {e}")
            return FetchResult(
                url=feed.url,
                success=False,
                entries=[],
                error=f"{type(e).__name__}: {e}",
            )

    async def get_feed(self, feed_id: int) -> Feed | None:
        """Get a feed by ID."""
        return await self.repo.get_feed_by_id(feed_id)

    async def get_feed_by_url(self, url: str) -> Feed | None:
        """Get a feed by URL."""
        return await self.repo.get_feed_by_url(url)

    async def delete_feed(self, feed_id: int) -> bool:
        """Delete a feed and all its entries."""
        return await self.repo.delete_feed(feed_id)

    async def get_recent_entries(
        self,
        feed_id: int,
        limit: int = 20,
    ) -> list[FeedEntry]:
        """Get recent entries for a feed."""
        entries = await self.repo.get_recent_entries(feed_id, limit)
        return list(entries)

    async def cleanup_old_entries(self, days: int = 7) -> int:
        """Cleanup old entries."""
        count = await self.repo.cleanup_old_entries(days)
        logger.info(f"Cleaned up {count} old entries")
        return count
