"""
RSS Feed Fetcher module.

Handles fetching and parsing RSS feeds with:
- Conditional requests (ETag, Last-Modified)
- Concurrent fetching with semaphore
- Error handling and retry logic
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import feedparser
from dateutil import parser as date_parser

from newsflow.config import get_settings

logger = logging.getLogger(__name__)

# Default headers for RSS requests
DEFAULT_HEADERS = {
    "User-Agent": "NewsFlow-Bot/1.0 (+https://github.com/newsflow-bot)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# Request timeout
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


@dataclass
class FetchResult:
    """Result of fetching an RSS feed."""

    url: str
    success: bool
    entries: list[dict[str, Any]]
    etag: str | None = None
    last_modified: str | None = None
    feed_title: str | None = None
    feed_description: str | None = None
    feed_link: str | None = None
    error: str | None = None
    not_modified: bool = False


@dataclass
class ParsedEntry:
    """Parsed RSS entry with normalized fields."""

    guid: str
    title: str
    link: str
    summary: str | None
    content: str | None
    author: str | None
    published_at: datetime | None
    image_url: str | None


class FeedFetcher:
    """
    Async RSS feed fetcher with caching support.

    Features:
    - Conditional requests to reduce bandwidth
    - Concurrent fetching with configurable parallelism
    - Robust error handling
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
    ):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=DEFAULT_HEADERS,
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def fetch_feed(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """
        Fetch a single RSS feed.

        Args:
            url: The feed URL
            etag: Previous ETag for conditional request
            last_modified: Previous Last-Modified for conditional request

        Returns:
            FetchResult with entries and metadata
        """
        async with self._semaphore:
            return await self._do_fetch(url, etag, last_modified)

    async def _do_fetch(
        self,
        url: str,
        etag: str | None,
        last_modified: str | None,
    ) -> FetchResult:
        """Internal fetch implementation."""
        session = await self._get_session()

        # Build headers for conditional request
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            async with session.get(url, headers=headers) as response:
                # Handle 304 Not Modified
                if response.status == 304:
                    logger.debug(f"Feed not modified: {url}")
                    return FetchResult(
                        url=url,
                        success=True,
                        entries=[],
                        not_modified=True,
                        etag=etag,
                        last_modified=last_modified,
                    )

                # Check for errors
                if response.status >= 400:
                    error_msg = f"HTTP {response.status}: {response.reason}"
                    logger.warning(f"Failed to fetch {url}: {error_msg}")
                    return FetchResult(
                        url=url,
                        success=False,
                        entries=[],
                        error=error_msg,
                    )

                # Read and parse content
                content = await response.text()
                feed = feedparser.parse(content)

                # Check for parse errors
                if feed.bozo and not feed.entries:
                    error_msg = str(feed.bozo_exception)
                    logger.warning(f"Failed to parse {url}: {error_msg}")
                    return FetchResult(
                        url=url,
                        success=False,
                        entries=[],
                        error=f"Parse error: {error_msg}",
                    )

                # Extract entries
                entries = [self._parse_entry(entry, url) for entry in feed.entries]

                # Get new cache headers
                new_etag = response.headers.get("ETag")
                new_last_modified = response.headers.get("Last-Modified")

                # Get feed metadata
                feed_info = feed.feed
                return FetchResult(
                    url=url,
                    success=True,
                    entries=entries,
                    etag=new_etag,
                    last_modified=new_last_modified,
                    feed_title=feed_info.get("title"),
                    feed_description=feed_info.get("description"),
                    feed_link=feed_info.get("link"),
                )

        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {url}")
            return FetchResult(
                url=url,
                success=False,
                entries=[],
                error="Request timeout",
            )
        except aiohttp.ClientError as e:
            logger.warning(f"Network error fetching {url}: {e}")
            return FetchResult(
                url=url,
                success=False,
                entries=[],
                error=f"Network error: {str(e)}",
            )
        except Exception as e:
            logger.exception(f"Unexpected error fetching {url}: {e}")
            return FetchResult(
                url=url,
                success=False,
                entries=[],
                error=f"Unexpected error: {str(e)}",
            )

    def _parse_entry(self, entry: Any, feed_url: str) -> dict[str, Any]:
        """Parse a feedparser entry into a normalized dict."""
        # Get GUID (unique identifier)
        guid = (
            entry.get("id")
            or entry.get("guid")
            or entry.get("link")
            or f"{entry.get('title', '')}-{entry.get('published', '')}"
        )

        # Get content (prefer full content over summary)
        content = None
        if "content" in entry and entry.content:
            content = entry.content[0].get("value", "")

        summary = entry.get("summary") or entry.get("description") or ""

        # Parse published date
        published_at = self._parse_date(entry)

        # Extract image URL
        image_url = self._extract_image(entry)

        return {
            "guid": guid,
            "title": entry.get("title", "Untitled"),
            "link": entry.get("link", feed_url),
            "summary": summary,
            "content": content,
            "author": entry.get("author"),
            "published_at": published_at,
            "image_url": image_url,
        }

    def _parse_date(self, entry: Any) -> datetime | None:
        """Parse entry date to datetime."""
        # Try parsed time first
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # Try string parsing
        for field in ["published", "updated", "created"]:
            if field in entry and entry[field]:
                try:
                    return date_parser.parse(entry[field]).astimezone(timezone.utc)
                except (ValueError, TypeError):
                    continue

        return None

    def _extract_image(self, entry: Any) -> str | None:
        """Extract image URL from entry."""
        # Check media_content
        if "media_content" in entry:
            for media in entry.media_content:
                if media.get("medium") == "image" or media.get("type", "").startswith(
                    "image/"
                ):
                    return media.get("url")

        # Check media_thumbnail
        if "media_thumbnail" in entry and entry.media_thumbnail:
            return entry.media_thumbnail[0].get("url")

        # Check enclosures
        if "enclosures" in entry:
            for enclosure in entry.enclosures:
                if enclosure.get("type", "").startswith("image/"):
                    return enclosure.get("href") or enclosure.get("url")

        # Check links
        if "links" in entry:
            for link in entry.links:
                if link.get("type", "").startswith("image/"):
                    return link.get("href")

        return None

    async def fetch_multiple(
        self,
        feeds: list[dict[str, Any]],
    ) -> list[FetchResult]:
        """
        Fetch multiple feeds concurrently.

        Args:
            feeds: List of dicts with 'url', optional 'etag' and 'last_modified'

        Returns:
            List of FetchResults
        """
        tasks = [
            self.fetch_feed(
                feed["url"],
                feed.get("etag"),
                feed.get("last_modified"),
            )
            for feed in feeds
        ]
        return await asyncio.gather(*tasks, return_exceptions=False)


# Singleton instance
_fetcher: FeedFetcher | None = None


def get_fetcher() -> FeedFetcher:
    """Get the global FeedFetcher instance."""
    global _fetcher
    if _fetcher is None:
        settings = get_settings()
        _fetcher = FeedFetcher(max_concurrent=10)
    return _fetcher


async def close_fetcher() -> None:
    """Close the global FeedFetcher."""
    global _fetcher
    if _fetcher is not None:
        await _fetcher.close()
        _fetcher = None
