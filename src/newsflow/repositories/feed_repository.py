"""
Feed repository for database operations.
"""

import hashlib
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.digest import WEEKLY_WINDOW
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry
from newsflow.repositories._result import rowcount

logger = logging.getLogger(__name__)

# Column-length caps for untrusted feed-derived text (mirrors the model columns).
# Over-length values fail the INSERT on Postgres and overrun platform message
# limits on SQLite, so truncate at ingest.
_ENTRY_TITLE_CAP, _ENTRY_URL_CAP, _ENTRY_AUTHOR_CAP = 1024, 2048, 256
_FEED_TITLE_CAP, _FEED_HEADER_CAP, _FEED_URL_CAP = 512, 256, 2048


_SURROGATE = re.compile("[\ud800-\udfff]")


def _mend_surrogates(text: str) -> str:
    """Rejoin a surrogate pair split into two code points and mark a lone half with
    U+FFFD: no encoding can store a lone one, so its INSERT fails the feed's batch."""
    if not _SURROGATE.search(text):
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _storable(value: str | None, limit: int | None = None) -> str | None:
    """Feed text as every backend stores it: surrogates mended, NUL dropped (Postgres
    rejects it), cut to the column width."""
    if value is None:
        return None
    text = _mend_surrogates(value).replace("\x00", "")
    return text if limit is None else text[:limit]


# A guid's stored key stays within this many UTF-8 bytes, which also keeps the
# (feed_id, guid) index entries under Postgres's btree limit of about 2.7 KB.
_GUID_KEY_MAX_BYTES = 2048


def _guid_key(guid: str) -> str:
    """How a guid is stored and matched: as is when it fits, else a cut prefix plus a hash
    of the whole, so guids sharing a long prefix stay apart. NUL is kept: SQLite rows may
    hold one already, and changing a stored key re-sends its entry."""
    raw = _mend_surrogates(guid).encode()
    if len(raw) <= _GUID_KEY_MAX_BYTES:
        return raw.decode()
    digest = hashlib.sha256(raw).hexdigest()
    prefix = raw[: _GUID_KEY_MAX_BYTES - len(digest) - 1].decode(errors="ignore")
    return f"{prefix}#{digest}"


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
        config: dict[str, Any] | None = None,
    ) -> Feed:
        """Create a new feed. Feed-derived metadata is capped to its column
        width here just like the update path (update_feed_metadata) — an
        over-length remote title otherwise fails the very first INSERT on
        Postgres and the feed can never be added."""
        feed = Feed(
            url=url,
            title=_storable(title, _FEED_TITLE_CAP),
            description=description,  # Text column — no cap
            site_url=_storable(site_url, _FEED_URL_CAP),
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
            update_data["title"] = _storable(title, _FEED_TITLE_CAP)
        if description:
            update_data["description"] = _storable(description)  # Text column — no cap
        if etag:
            update_data["etag"] = _storable(etag, _FEED_HEADER_CAP)
        if last_modified:
            update_data["last_modified"] = _storable(last_modified, _FEED_HEADER_CAP)

        await self.session.execute(update(Feed).where(Feed.id == feed_id).values(**update_data))

    async def record_full_fetch(self, feed_id: int, guids: Sequence[str]) -> None:
        """Note a fetch that returned the whole document: when, and that every sent
        record among `guids` is still listed at the source."""
        now = datetime.now(UTC)
        await self.session.execute(
            update(Feed).where(Feed.id == feed_id).values(last_full_fetch_at=now)
        )
        keys = list({_guid_key(guid) for guid in guids})
        if keys:
            await self.session.execute(
                update(SentEntry)
                .where(SentEntry.feed_id == feed_id, SentEntry.guid.in_(keys))
                .values(last_seen_at=now)
            )

    async def mark_feed_error(
        self,
        feed_id: int,
        error: str | None,
        base_delay_seconds: int = 3600,
        status: int | None = None,
    ) -> None:
        """Mark a feed fetch error and schedule the retry (see Feed.mark_error)."""
        feed = await self.get_feed_by_id(feed_id)
        if feed:
            feed.mark_error(error, base_delay_seconds=base_delay_seconds, status=status)

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

    async def create_entries_bulk(
        self,
        feed_id: int,
        entries_data: list[dict[str, Any]],
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

        # Key the existence check exactly as rows are stored.
        guids = [_guid_key(data["guid"]) for data in entries_data]
        result = await self.session.execute(
            select(FeedEntry.guid).where(
                FeedEntry.feed_id == feed_id,
                FeedEntry.guid.in_(guids),
            )
        )
        existing_guids = set(result.scalars().all())

        # Deduplicate within the batch as well as against the DB: one fetch can return a
        # guid twice, and the resulting IntegrityError on flush would poison the shared
        # session for the rest of the dispatch cycle.
        now = datetime.now(UTC)
        seen: set[str] = set()
        new_entries: list[FeedEntry] = []
        for data, guid in zip(entries_data, guids):
            if guid in existing_guids or guid in seen:
                continue
            seen.add(guid)
            new_entries.append(
                FeedEntry(
                    feed_id=feed_id,
                    guid=guid,
                    title=_storable(data["title"], _ENTRY_TITLE_CAP),
                    link=_storable(data["link"], _ENTRY_URL_CAP),
                    summary=_storable(data.get("summary")),
                    content=_storable(data.get("content")),
                    author=_storable(data.get("author"), _ENTRY_AUTHOR_CAP),
                    published_at=_clamp_future_date(data.get("published_at"), now),
                    image_url=_storable(data.get("image_url"), _ENTRY_URL_CAP),
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
        """Update entry with translation. The translated title is capped to
        its column width — providers can expand text past the original's
        length, and Postgres rejects over-length values outright."""
        await self.session.execute(
            update(FeedEntry)
            .where(FeedEntry.id == entry_id)
            .values(
                title_translated=title_translated[:_ENTRY_TITLE_CAP],
                summary_translated=summary_translated,
                translation_language=language,
            )
        )

    async def cleanup_old_entries(self, days: int = 7) -> int:
        """Delete entries stored more than `days` ago, except those a channel processed
        within a digest window: a digest selects by when an entry was processed, and
        one processed late (a paused subscription resumed) would lose its body first.

        Returns:
            Number of deleted entries
        """
        now = datetime.now(UTC)
        digest_material = (
            select(FeedEntry.id)
            .join(
                SentEntry,
                (SentEntry.feed_id == FeedEntry.feed_id) & (SentEntry.guid == FeedEntry.guid),
            )
            .where(SentEntry.sent_at > now - WEEKLY_WINDOW, SentEntry.seeded.is_(False))
        )
        result = await self.session.execute(
            delete(FeedEntry).where(
                FeedEntry.created_at < now - timedelta(days=days),
                FeedEntry.id.not_in(digest_material),
            )
        )
        return rowcount(result)

    async def count_entries(self, feed_id: int) -> int:
        """Count entries for a feed."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(FeedEntry.id)).where(FeedEntry.feed_id == feed_id)
        )
        return result.scalar_one()
