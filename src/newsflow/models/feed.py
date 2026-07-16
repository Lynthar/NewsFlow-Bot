"""
Feed and FeedEntry models for RSS sources.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from newsflow.models.base import Base

if TYPE_CHECKING:
    from newsflow.models.subscription import Subscription


class Feed(Base):
    """
    RSS Feed source.

    Stores information about an RSS feed URL and its metadata
    for efficient fetching (ETag, Last-Modified).
    """

    __tablename__ = "feeds"

    # Feed URL (unique identifier)
    url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)

    # Feed metadata (from RSS)
    title: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    site_url: Mapped[str | None] = mapped_column(String(2048))

    # Source type selects the fetcher. 'rss' is the default and keeps every
    # existing feed on the optimized RSS batch path; other values (json_api,
    # email_imap, …) route through a registered SourceFetcher. `config` holds
    # source-specific settings (JSONPath mappings, IMAP target, …) in a generic
    # SQLAlchemy JSON column: TEXT on SQLite, a JSON column on Postgres (not
    # JSONB — the blob is stored/loaded whole, never queried into). NULL for
    # plain RSS.
    source_type: Mapped[str] = mapped_column(
        String(32), default="rss", server_default="rss", nullable=False
    )
    config: Mapped[dict | None] = mapped_column(JSON)

    # Feed status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    error_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    # HTTP caching headers for conditional requests
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(String(256))

    # Fetch tracking
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Exponential backoff: while set and in the future, the dispatcher skips
    # this feed. Cleared on successful fetch; pushed further on each error.
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    entries: Mapped[list["FeedEntry"]] = relationship(
        back_populates="feed",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="feed",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Feed(id={self.id}, url='{self.url[:50]}...')>"

    def mark_success(self, etag: str | None = None, last_modified: str | None = None) -> None:
        """Mark a successful fetch."""
        now = datetime.now(UTC)
        self.last_fetched_at = now
        self.last_successful_fetch_at = now
        self.error_count = 0
        self.last_error = None
        self.next_retry_at = None
        if etag:
            self.etag = etag
        if last_modified:
            self.last_modified = last_modified

    def mark_error(self, error: str | None, base_delay_seconds: int = 3600) -> None:
        """Record a failed fetch and schedule the next retry with exponential
        backoff: delay = base_delay * 2^min(error_count, 5), capped so we
        don't overshoot before the error_count=10 auto-deactivate kicks in.
        """
        now = datetime.now(UTC)
        self.last_fetched_at = now
        self.error_count += 1
        self.last_error = error

        factor = 2 ** min(self.error_count, 5)
        self.next_retry_at = now + timedelta(seconds=base_delay_seconds * factor)

        if self.error_count >= 10:
            self.is_active = False

    def reactivate(self) -> None:
        """Give a (possibly auto-disabled) feed a fresh chance: clear the
        error streak and backoff so the next dispatch cycle fetches it again.

        This is the only revival path for a feed that mark_error() disabled —
        get_feeds_due_for_fetch skips inactive feeds, so mark_success() can
        never run for them. Called when a user resumes/re-adds a subscription
        or a YAML sync re-declares the feed. `last_error` is kept for
        /feed status history until the next fetch outcome overwrites it.
        """
        self.is_active = True
        self.error_count = 0
        self.next_retry_at = None


class FeedEntry(Base):
    """
    Individual RSS entry/article.

    Stores the content and translation cache for each entry.
    """

    __tablename__ = "feed_entries"

    # Foreign key to Feed
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"))

    # Entry identifier (GUID from RSS, or generated)
    guid: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Original content
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    link: Mapped[str] = mapped_column(String(2048), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)  # Full content if available
    author: Mapped[str | None] = mapped_column(String(256))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Media
    image_url: Mapped[str | None] = mapped_column(String(2048))

    # Translation cache
    title_translated: Mapped[str | None] = mapped_column(String(1024))
    summary_translated: Mapped[str | None] = mapped_column(Text)
    translation_language: Mapped[str | None] = mapped_column(String(10))

    # Relationship
    feed: Mapped["Feed"] = relationship(back_populates="entries")

    # Indexes for efficient queries
    __table_args__ = (
        Index("ix_feed_entries_feed_guid", "feed_id", "guid", unique=True),
        Index("ix_feed_entries_published", "published_at"),
    )

    def __repr__(self) -> str:
        return f"<FeedEntry(id={self.id}, title='{self.title[:30]}...')>"
