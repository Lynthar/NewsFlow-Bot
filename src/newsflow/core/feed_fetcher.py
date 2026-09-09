"""
RSS Feed Fetcher module.

Handles fetching and parsing RSS feeds with:
- Conditional requests (ETag, Last-Modified)
- Concurrent fetching with semaphore
- Error handling and retry logic
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urljoin

import aiohttp
import feedparser
from dateutil import parser as date_parser

from newsflow.config import get_settings
from newsflow.core.url_security import InvalidFeedURLError, validate_feed_url

logger = logging.getLogger(__name__)

# Default headers for RSS requests
DEFAULT_HEADERS = {
    "User-Agent": "NewsFlow-Bot/1.0 (+https://github.com/Lynthar/NewsFlow-Bot)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Encoding": "gzip, deflate",
}

# Request timeout
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# Cap the raw body we'll accept from a feed. Normal RSS is well under 1 MiB;
# anything much larger is either a misconfiguration or an attempt to make us
# read (and feedparser parse) an unbounded amount of memory.
MAX_FEED_SIZE_BYTES = 5 * 1024 * 1024

# Max redirect hops. Every hop is re-validated by validate_feed_url before we
# connect: aiohttp's default following would chase a Location header into a
# private / loopback / metadata address even when the initial URL was vetted.
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Streaming read granularity for read_body_capped.
_READ_CHUNK_BYTES = 64 * 1024


async def read_body_capped(
    content: aiohttp.StreamReader, cap: int = MAX_FEED_SIZE_BYTES
) -> bytes | None:
    """Read the whole response body, or None when it exceeds `cap`.

    Never size-cap with ``StreamReader.read(n)``: it returns only what is
    already buffered, so a body arriving in several chunks gets truncated
    silently and a large feed then parses into a few entries with no error.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in content.iter_chunked(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class FeedFetchError(Exception):
    """A fetch failed before any usable body could be read."""


@asynccontextmanager
async def follow_redirects(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Yield the first non-redirect response, re-validating every hop through
    validate_feed_url: aiohttp's own following would chase a Location header into
    a private or metadata address. `url` itself is the caller's to validate.

    Raises:
        FeedFetchError: a redirect target failed the SSRF allow-list, a 3xx
            carried no Location, or the hop budget ran out.
    """
    current_url = url
    for _hop in range(MAX_REDIRECTS + 1):
        async with session.get(current_url, headers=headers, allow_redirects=False) as response:
            if response.status not in REDIRECT_STATUSES:
                yield response
                return
            location = response.headers.get("Location")
            if not location:
                raise FeedFetchError(f"HTTP {response.status} redirect without Location header")
            next_url = urljoin(current_url, location)
            try:
                validate_feed_url(next_url)
            except InvalidFeedURLError as e:
                logger.warning(f"Rejected redirect from {url!r} to {next_url!r}: {e}")
                raise FeedFetchError(f"Unsafe redirect target: {e}") from e
            current_url = next_url
    logger.warning(f"Too many redirects fetching {url}")
    raise FeedFetchError(f"Too many redirects (>{MAX_REDIRECTS})")


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
    # Feed URLs advertised by an HTML page via <link rel="alternate"> when
    # `url` turned out not to be a feed. add_feed resolves and retries against
    # these. Empty for normal feed responses.
    discovered_feeds: list[str] = field(default_factory=list)


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
        try:
            validate_feed_url(url)
        except InvalidFeedURLError as e:
            logger.warning(f"Rejected feed URL {url!r}: {e}")
            return FetchResult(url=url, success=False, entries=[], error=str(e))

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
            async with follow_redirects(session, url, headers) as response:
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

                # Refuse the response up-front if Content-Length is too large.
                if (
                    response.content_length is not None
                    and response.content_length > MAX_FEED_SIZE_BYTES
                ):
                    logger.warning(
                        f"Feed {url} too large: {response.content_length} > {MAX_FEED_SIZE_BYTES}"
                    )
                    return FetchResult(
                        url=url,
                        success=False,
                        entries=[],
                        error=(f"Feed exceeds size limit ({response.content_length} bytes)"),
                    )

                # Read streaming, capped. A server that lies about
                # Content-Length (or omits it) can't drain our memory.
                raw = await read_body_capped(response.content)
                if raw is None:
                    logger.warning(f"Feed {url} exceeded size limit mid-stream")
                    return FetchResult(
                        url=url,
                        success=False,
                        entries=[],
                        error="Feed exceeds size limit",
                    )

                content = raw.decode(response.charset or "utf-8", errors="replace")

                # JSON Feed: feedparser only parses XML, so detect and map it here. Detection is
                # conservative (official content-type or a jsonfeed.org version marker) so XML
                # never enters this branch.
                json_feed = self._parse_json_feed(content, response.content_type, url)
                if json_feed is not None:
                    json_entries, json_title = json_feed
                    return FetchResult(
                        url=url,
                        success=True,
                        entries=json_entries,
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                        feed_title=json_title,
                    )

                feed = feedparser.parse(content)

                # If the body was an HTML page advertising a feed (<link rel="alternate">, which
                # feedparser surfaces in feed.feed.links), hand those back so add_feed can
                # resolve and retry the real URL.
                if feed.bozo and not feed.entries:
                    error_msg = str(feed.bozo_exception)
                    logger.warning(f"Failed to parse {url}: {error_msg}")
                    return FetchResult(
                        url=url,
                        success=False,
                        entries=[],
                        error=f"Parse error: {error_msg}",
                        discovered_feeds=self._discover_feeds(feed, url),
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

        except FeedFetchError as e:
            return FetchResult(url=url, success=False, entries=[], error=str(e))
        except TimeoutError:
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

    async def fetch_bytes_capped(self, url: str, cap: int = MAX_FEED_SIZE_BYTES) -> bytes | None:
        """Fetch a whole response body, or None when it is larger than `cap`.
        Streams instead of StreamReader.read(n), which returns only what is
        buffered and truncates a chunked response silently; hops are SSRF-checked.

        Raises:
            InvalidFeedURLError: `url` itself is malformed or unsafe.
            FeedFetchError: HTTP >= 400, an unsafe or Location-less redirect,
                or more than MAX_REDIRECTS hops.
        """
        validate_feed_url(url)
        async with self._semaphore:
            session = await self._get_session()
            async with follow_redirects(session, url) as response:
                if response.status >= 400:
                    raise FeedFetchError(f"HTTP {response.status}: {response.reason}")
                if response.content_length is not None and response.content_length > cap:
                    return None
                return await read_body_capped(response.content, cap)

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

    def _discover_feeds(self, feed: Any, base_url: str) -> list[str]:
        """Return feed URLs an HTML page advertises via ``<link rel="alternate">``.

        feedparser surfaces these in ``feed.feed.links`` even when the page
        itself is not a feed. Each candidate is resolved to an absolute URL and
        must pass the SSRF allow-list (``validate_feed_url``) to be returned.
        """
        feed_types = {
            "application/rss+xml",
            "application/atom+xml",
            "application/feed+json",
            "application/json",
        }
        out: list[str] = []
        for link in (feed.feed or {}).get("links") or []:
            if link.get("rel") != "alternate" or link.get("type") not in feed_types:
                continue
            href = link.get("href")
            if not href:
                continue
            candidate = urljoin(base_url, href)
            try:
                validate_feed_url(candidate)
            except InvalidFeedURLError:
                continue
            if candidate not in out:
                out.append(candidate)
        return out

    def _parse_json_feed(
        self, content: str, content_type: str | None, feed_url: str
    ) -> tuple[list[dict[str, Any]], str | None] | None:
        """Parse a JSON Feed body into ``(entries, feed_title)``.

        Returns None when the body is not a JSON Feed, so the caller falls back
        to the XML (feedparser) path. Detection is conservative: the official
        ``application/feed+json`` content-type, or a sniff for the jsonfeed.org
        version marker — an XML feed never matches.
        """
        head = content[:1000].lstrip()
        looks_json = content_type == "application/feed+json" or (
            head.startswith("{") and "jsonfeed.org/version" in content[:1000]
        )
        if not looks_json:
            return None
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return None
        entries = [
            self._json_feed_item(item, feed_url) for item in data["items"] if isinstance(item, dict)
        ]
        title = data.get("title")
        return entries, title if isinstance(title, str) else None

    def _json_feed_item(self, item: dict[str, Any], feed_url: str) -> dict[str, Any]:
        """Map one JSON Feed item to the normalized dict ``_parse_entry`` yields."""
        url = item.get("url") or item.get("external_url")
        # JSON Feed requires a unique `id`; fall back to the url, then to a
        # content hash so multiple id-less items can't collapse to one guid
        # (which would make dedupe drop all but the first).
        guid = item.get("id") or url
        if not guid:
            basis = (
                f"{item.get('title', '')}{item.get('content_text', '')}"
                f"{item.get('content_html', '')}"
            )
            guid = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        # authors[] in JSON Feed 1.1; author{} in 1.0.
        authors = item.get("authors")
        if not authors and isinstance(item.get("author"), dict):
            authors = [item["author"]]
        author = None
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            author = authors[0].get("name")
        # Reuse _parse_date's naive→UTC string handling via a feedparser-shaped
        # dict, rather than duplicating it here.
        return {
            "guid": str(guid),
            "title": item.get("title") or "Untitled",
            "link": url or feed_url,
            "summary": item.get("content_text") or "",
            "content": item.get("content_html"),
            "author": author,
            "published_at": self._parse_date({"published": item.get("date_published")}),
            "image_url": item.get("image") or item.get("banner_image"),
        }

    def _parse_date(self, entry: Any) -> datetime | None:
        """Parse entry date to datetime.

        Also serves JSON Feed: callers hand a ``{"published": <rfc3339>}`` dict
        so the naive→UTC string handling below is shared, not duplicated.
        """
        # Try parsed time first
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                return datetime(
                    *cast("tuple[int, int, int, int, int, int]", entry.published_parsed[:6]),
                    tzinfo=UTC,
                )
            except (ValueError, TypeError):
                pass

        # Try string parsing
        for key in ["published", "updated", "created"]:
            if key in entry and entry[key]:
                try:
                    dt: datetime = date_parser.parse(entry[key])
                    # A date string with no offset parses to a naive datetime;
                    # .astimezone() would then assume the *host's* local tz.
                    # Treat naive as UTC, matching the published_parsed branch.
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    return dt.astimezone(UTC)
                except (ValueError, TypeError):
                    continue

        return None

    def _extract_image(self, entry: Any) -> str | None:
        """Extract image URL from entry."""
        # Check media_content
        if "media_content" in entry:
            for media in entry.media_content:
                if media.get("medium") == "image" or media.get("type", "").startswith("image/"):
                    return cast("str | None", media.get("url"))

        # Check media_thumbnail
        if "media_thumbnail" in entry and entry.media_thumbnail:
            return cast("str | None", entry.media_thumbnail[0].get("url"))

        # Check enclosures
        if "enclosures" in entry:
            for enclosure in entry.enclosures:
                if enclosure.get("type", "").startswith("image/"):
                    return cast("str | None", enclosure.get("href") or enclosure.get("url"))

        # Check links
        if "links" in entry:
            for link in entry.links:
                if link.get("type", "").startswith("image/"):
                    return cast("str | None", link.get("href"))

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
        _fetcher = FeedFetcher(max_concurrent=get_settings().feed_max_concurrent)
    return _fetcher


async def close_fetcher() -> None:
    """Close the global FeedFetcher."""
    global _fetcher
    if _fetcher is not None:
        await _fetcher.close()
        _fetcher = None
