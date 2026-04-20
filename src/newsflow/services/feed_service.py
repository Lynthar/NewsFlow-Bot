"""
Feed service - Business logic for feed management.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.config import get_settings
from newsflow.core.content_processor import process_content
from newsflow.core.feed_fetcher import FeedFetcher, FetchResult, get_fetcher
from newsflow.models.feed import Feed, FeedEntry
from newsflow.repositories.feed_repository import FeedRepository

logger = logging.getLogger(__name__)


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
    new_entries: list[FeedEntry] = None
    message: str = ""

    def __post_init__(self):
        if self.new_entries is None:
            self.new_entries = []


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
        # Check if feed already exists
        existing = await self.repo.get_feed_by_url(url)
        if existing:
            return AddFeedResult(
                success=True,
                feed=existing,
                message="Feed already exists",
            )

        # Fetch and validate feed
        result = await self.fetcher.fetch_feed(url)
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

        # Create feed record
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

        logger.info(f"Added feed: {url} with {len(entries)} entries")

        return AddFeedResult(
            success=True,
            feed=feed,
            message=f"Feed added with {len(entries)} entries",
            entry_count=len(entries),
        )

    async def test_feed(self, url: str) -> FetchResult:
        """
        Test if a feed URL is valid.

        Args:
            url: The RSS feed URL

        Returns:
            FetchResult with success status and entries
        """
        return await self.fetcher.fetch_feed(url)

    async def _apply_fetch_result(
        self, feed: Feed, result: FetchResult
    ) -> FetchFeedResult:
        """Write a FetchResult to the DB. No network I/O — safe to call
        sequentially over a batch of already-fetched results."""
        if not result.success:
            await self.repo.mark_feed_error(
                feed.id, result.error, base_delay_seconds=self._backoff_base_seconds
            )
            return FetchFeedResult(
                success=False,
                feed=feed,
                message=f"Fetch error: {result.error}",
            )

        if result.not_modified:
            await self.repo.update_feed_metadata(feed.id)
            return FetchFeedResult(
                success=True, feed=feed, message="Not modified"
            )

        await self.repo.update_feed_metadata(
            feed_id=feed.id,
            title=result.feed_title,
            description=result.feed_description,
            etag=result.etag,
            last_modified=result.last_modified,
        )

        if result.entries:
            new_entries = await self.repo.create_entries_bulk(
                feed.id, result.entries
            )
            logger.info(f"Feed {feed.url}: {len(new_entries)} new entries")
            return FetchFeedResult(
                success=True,
                feed=feed,
                new_entries=new_entries,
                message=f"{len(new_entries)} new entries",
            )

        return FetchFeedResult(
            success=True, feed=feed, message="No new entries"
        )

    async def fetch_and_store(self, feed: Feed) -> FetchFeedResult:
        """
        Fetch a single feed and store new entries. Used by single-feed
        callers (e.g. the API `/refresh` endpoint).
        """
        try:
            result = await self.fetcher.fetch_feed(
                url=feed.url,
                etag=feed.etag,
                last_modified=feed.last_modified,
            )
            return await self._apply_fetch_result(feed, result)
        except Exception as e:
            logger.exception(f"Error fetching feed {feed.url}: {e}")
            await self.repo.mark_feed_error(
                feed.id, str(e), base_delay_seconds=self._backoff_base_seconds
            )
            return FetchFeedResult(
                success=False, feed=feed, message=f"Error: {str(e)}"
            )

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

        fetch_results = await self.fetcher.fetch_multiple(
            [
                {
                    "url": f.url,
                    "etag": f.etag,
                    "last_modified": f.last_modified,
                }
                for f in feeds
            ]
        )

        results: list[FetchFeedResult] = []
        for feed, fr in zip(feeds, fetch_results):
            try:
                results.append(await self._apply_fetch_result(feed, fr))
            except Exception as e:
                logger.exception(
                    f"Error applying fetch result for {feed.url}: {e}"
                )
                await self.repo.mark_feed_error(
                feed.id, str(e), base_delay_seconds=self._backoff_base_seconds
            )
                results.append(
                    FetchFeedResult(
                        success=False,
                        feed=feed,
                        message=f"Error: {str(e)}",
                    )
                )

        return results

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
