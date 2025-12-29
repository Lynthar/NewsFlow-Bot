"""
Feed repository for database operations.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.feed import Feed, FeedEntry

logger = logging.getLogger(__name__)


class FeedRepository:
    """
    Repository for Feed and FeedEntry operations.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ===== Feed Operations =====

    async def get_feed_by_id(self, feed_id: int) -> Feed | None:
        """Get a feed by ID."""
        result = await self.session.execute(
            select(Feed).where(Feed.id == feed_id)
        )
        return result.scalar_one_or_none()

    async def get_feed_by_url(self, url: str) -> Feed | None:
        """Get a feed by URL."""
        result = await self.session.execute(
            select(Feed).where(Feed.url == url)
        )
        return result.scalar_one_or_none()

    async def get_all_active_feeds(self) -> Sequence[Feed]:
        """Get all active feeds."""
        result = await self.session.execute(
            select(Feed).where(Feed.is_active == True)
        )
        return result.scalars().all()

    async def get_feeds_needing_fetch(
        self,
        interval_minutes: int = 60,
    ) -> Sequence[Feed]:
        """Get feeds that need to be fetched."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=interval_minutes)
        result = await self.session.execute(
            select(Feed).where(
                Feed.is_active == True,
                (Feed.last_fetched_at == None) | (Feed.last_fetched_at < cutoff),
            )
        )
        return result.scalars().all()

    async def create_feed(
        self,
        url: str,
        title: str | None = None,
        description: str | None = None,
        site_url: str | None = None,
    ) -> Feed:
        """Create a new feed."""
        feed = Feed(
            url=url,
            title=title,
            description=description,
            site_url=site_url,
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
        """Update feed metadata after successful fetch."""
        update_data = {
            "last_fetched_at": datetime.now(timezone.utc),
            "last_successful_fetch_at": datetime.now(timezone.utc),
            "error_count": 0,
            "last_error": None,
        }
        if title:
            update_data["title"] = title
        if description:
            update_data["description"] = description
        if etag:
            update_data["etag"] = etag
        if last_modified:
            update_data["last_modified"] = last_modified

        await self.session.execute(
            update(Feed).where(Feed.id == feed_id).values(**update_data)
        )

    async def mark_feed_error(self, feed_id: int, error: str) -> None:
        """Mark a feed fetch error."""
        feed = await self.get_feed_by_id(feed_id)
        if feed:
            feed.mark_error(error)

    async def delete_feed(self, feed_id: int) -> bool:
        """Delete a feed and all its entries."""
        result = await self.session.execute(
            delete(Feed).where(Feed.id == feed_id)
        )
        return result.rowcount > 0

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

    async def get_unsent_entries(self, feed_id: int, limit: int = 50) -> Sequence[FeedEntry]:
        """Get entries that haven't been sent yet."""
        result = await self.session.execute(
            select(FeedEntry)
            .where(
                FeedEntry.feed_id == feed_id,
                FeedEntry.is_sent == False,
            )
            .order_by(FeedEntry.published_at.desc().nullslast())
            .limit(limit)
        )
        return result.scalars().all()

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
        created = []
        for data in entries_data:
            # Skip if exists
            existing = await self.get_entry_by_guid(feed_id, data["guid"])
            if existing:
                continue

            entry = await self.create_entry(
                feed_id=feed_id,
                guid=data["guid"],
                title=data["title"],
                link=data["link"],
                summary=data.get("summary"),
                content=data.get("content"),
                author=data.get("author"),
                published_at=data.get("published_at"),
                image_url=data.get("image_url"),
            )
            created.append(entry)

        return created

    async def mark_entry_sent(self, entry_id: int) -> None:
        """Mark an entry as sent."""
        await self.session.execute(
            update(FeedEntry).where(FeedEntry.id == entry_id).values(is_sent=True)
        )

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
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await self.session.execute(
            delete(FeedEntry).where(FeedEntry.created_at < cutoff)
        )
        return result.rowcount

    async def count_entries(self, feed_id: int) -> int:
        """Count entries for a feed."""
        from sqlalchemy import func
        result = await self.session.execute(
            select(func.count(FeedEntry.id)).where(FeedEntry.feed_id == feed_id)
        )
        return result.scalar_one()
