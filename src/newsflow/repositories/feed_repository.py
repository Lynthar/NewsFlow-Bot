"""
Feed repository for database operations.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.feed import Feed, FeedEntry
from newsflow.repositories._result import rowcount

logger = logging.getLogger(__name__)

# Column-length caps for untrusted feed-derived text (mirrors the FeedEntry /
# Feed model columns). Feeds are untrusted and some serve titles/URLs longer
# than the column limit: on Postgres an over-length value fails the INSERT with
# StringDataRightTruncationError and takes the whole feed's fetch down; on
# SQLite it stores but the oversized text then overruns platform message limits
# at render. Truncate at ingest so neither can happen.
_ENTRY_TITLE_CAP, _ENTRY_URL_CAP, _ENTRY_AUTHOR_CAP = 1024, 2048, 256
_FEED_TITLE_CAP, _FEED_HEADER_CAP = 512, 256


def _cap(value: str | None, limit: int) -> str | None:
    """None-safe truncation for optional text fields."""
    return value[:limit] if value is not None else None


def _clamp_future_date(published_at: datetime | None, now: datetime) -> datetime | None:
    """Clamp a clearly-future published_at (more than a day ahead) to `now`.

    A broken or hostile feed can stamp entries far in the future; left as-is the
    entry shows an absurd timestamp and sorts as the newest item forever-first
    in the backlog. Only dates >1 day ahead are clamped, so a legitimately
    timezone-skewed near-future entry is left untouched."""
    if published_at is None:
        return None
    aware = published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC)
    return now if aware > now + timedelta(days=1) else published_at


class FeedRepository:
    """
    Repository for Feed and FeedEntry operations.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ===== Feed Operations =====

    async def get_feed_by_id(self, feed_id: int) -> Feed | None:
        """Get a feed by ID."""
        result = await self.session.execute(select(Feed).where(Feed.id == feed_id))
        return result.scalar_one_or_none()

    async def get_feed_by_url(self, url: str) -> Feed | None:
        """Get a feed by URL."""
        result = await self.session.execute(select(Feed).where(Feed.url == url))
        return result.scalar_one_or_none()

    async def get_all_active_feeds(self) -> Sequence[Feed]:
        """Get all active feeds."""
        result = await self.session.execute(select(Feed).where(Feed.is_active.is_(True)))
        return result.scalars().all()

    async def get_feeds_due_for_fetch(self) -> Sequence[Feed]:
        """Active feeds that aren't currently inside a backoff window."""
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(Feed).where(
                Feed.is_active.is_(True),
                or_(Feed.next_retry_at.is_(None), Feed.next_retry_at <= now),
            )
        )
        return result.scalars().all()

    async def create_feed(
        self,
        url: str,
        title: str | None = None,
        description: str | None = None,
        site_url: str | None = None,
        source_type: str = "rss",
        config: dict | None = None,
    ) -> Feed:
        """Create a new feed."""
        feed = Feed(
            url=url,
            title=title,
            description=description,
            site_url=site_url,
            source_type=source_type,
            config=config,
        )
        self.session.add(feed)
        await self.session.flush()
        await self.session.refresh(feed)
        return feed

    async def get_or_create_feed(
        self,
        url: str,
        title: str | None = None,
        description: str | None = None,
    ) -> tuple[Feed, bool]:
        """
        Get existing feed or create new one.

        Returns:
            Tuple of (feed, created) where created is True if new feed was created.
        """
        existing = await self.get_feed_by_url(url)
        if existing:
            return existing, False

        feed = await self.create_feed(url, title, description)
        return feed, True

    async def update_feed_metadata(
        self,
        feed_id: int,
        title: str | None = None,
        description: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Update feed metadata after successful fetch. Clears any pending
        backoff — a success means we're back in good standing."""
        update_data = {
            "last_fetched_at": datetime.now(UTC),
            "last_successful_fetch_at": datetime.now(UTC),
            "error_count": 0,
            "last_error": None,
            "next_retry_at": None,
        }
        if title:
            update_data["title"] = title[:_FEED_TITLE_CAP]
        if description:
            update_data["description"] = description  # Text column — no cap
        if etag:
            update_data["etag"] = etag[:_FEED_HEADER_CAP]
        if last_modified:
            update_data["last_modified"] = last_modified[:_FEED_HEADER_CAP]

        await self.session.execute(update(Feed).where(Feed.id == feed_id).values(**update_data))

    async def mark_feed_error(
        self, feed_id: int, error: str | None, base_delay_seconds: int = 3600
    ) -> None:
        """Mark a feed fetch error, scheduling exponential backoff."""
        feed = await self.get_feed_by_id(feed_id)
        if feed:
            feed.mark_error(error, base_delay_seconds=base_delay_seconds)

    async def delete_feed(self, feed_id: int) -> bool:
        """Delete a feed and all its entries."""
        result = await self.session.execute(delete(Feed).where(Feed.id == feed_id))
        return rowcount(result) > 0

    # ===== FeedEntry Operations =====

    async def get_entry_by_guid(self, feed_id: int, guid: str) -> FeedEntry | None:
        """Get an entry by feed ID and GUID."""
        result = await self.session.execute(
            select(FeedEntry).where(
                FeedEntry.feed_id == feed_id,
                FeedEntry.guid == guid,
            )
        )
        return result.scalar_one_or_none()

    async def get_recent_entries(
        self,
        feed_id: int,
        limit: int = 20,
    ) -> Sequence[FeedEntry]:
        """Get recent entries for a feed."""
        result = await self.session.execute(
            select(FeedEntry)
            .where(FeedEntry.feed_id == feed_id)
            .order_by(FeedEntry.published_at.desc().nullslast())
            .limit(limit)
        )
        return result.scalars().all()

    async def create_entry(
        self,
        feed_id: int,
        guid: str,
        title: str,
        link: str,
        summary: str | None = None,
        content: str | None = None,
        author: str | None = None,
        published_at: datetime | None = None,
        image_url: str | None = None,
    ) -> FeedEntry:
        """Create a new feed entry."""
        entry = FeedEntry(
            feed_id=feed_id,
            guid=guid,
            title=title,
            link=link,
            summary=summary,
            content=content,
            author=author,
            published_at=published_at,
            image_url=image_url,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def create_entries_bulk(
        self,
        feed_id: int,
        entries_data: list[dict],
    ) -> list[FeedEntry]:
        """
        Bulk create entries, skipping existing ones.

        Args:
            feed_id: The feed ID
            entries_data: List of entry dicts with keys:
                guid, title, link, summary, content, author, published_at, image_url

        Returns:
            List of newly created entries
        """
        if not entries_data:
            return []

        # Truncate guid to its column length up front so the existence check
        # matches previously-stored (also-truncated) rows — otherwise a
        # >2048-char guid would miss the check and then collide on INSERT.
        guids = [data["guid"][:_ENTRY_URL_CAP] for data in entries_data]
        result = await self.session.execute(
            select(FeedEntry.guid).where(
                FeedEntry.feed_id == feed_id,
                FeedEntry.guid.in_(guids),
            )
        )
        existing_guids = set(result.scalars().all())

        # Deduplicate within this batch as well as against the DB. A single
        # fetch can return the same guid twice — either legitimately, or via
        # the degenerate `"{title}-{published}"` guid fallback in
        # FeedFetcher._parse_entry when entries carry no id/guid/link. Inserting
        # both rows would violate the (feed_id, guid) unique index on flush, and
        # that IntegrityError would poison the shared session for the rest of
        # the dispatch cycle (every other feed's metadata/backoff updates and
        # pending SentEntry writes would be rolled back).
        now = datetime.now(UTC)
        seen: set[str] = set()
        new_entries: list[FeedEntry] = []
        for data in entries_data:
            guid = data["guid"][:_ENTRY_URL_CAP]
            if guid in existing_guids or guid in seen:
                continue
            seen.add(guid)
            new_entries.append(
                FeedEntry(
                    feed_id=feed_id,
                    guid=guid,
                    title=data["title"][:_ENTRY_TITLE_CAP],
                    link=data["link"][:_ENTRY_URL_CAP],
                    summary=data.get("summary"),
                    content=data.get("content"),
                    author=_cap(data.get("author"), _ENTRY_AUTHOR_CAP),
                    published_at=_clamp_future_date(data.get("published_at"), now),
                    image_url=_cap(data.get("image_url"), _ENTRY_URL_CAP),
                )
            )

        if new_entries:
            self.session.add_all(new_entries)
            await self.session.flush()

        return new_entries

    async def update_entry_translation(
        self,
        entry_id: int,
        title_translated: str,
        summary_translated: str,
        language: str,
    ) -> None:
        """Update entry with translation."""
        await self.session.execute(
            update(FeedEntry)
            .where(FeedEntry.id == entry_id)
            .values(
                title_translated=title_translated,
                summary_translated=summary_translated,
                translation_language=language,
            )
        )

    async def cleanup_old_entries(self, days: int = 7) -> int:
        """
        Delete entries older than specified days.

        Returns:
            Number of deleted entries
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.execute(delete(FeedEntry).where(FeedEntry.created_at < cutoff))
        return rowcount(result)

    async def count_entries(self, feed_id: int) -> int:
        """Count entries for a feed."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(FeedEntry.id)).where(FeedEntry.feed_id == feed_id)
        )
        return result.scalar_one()
