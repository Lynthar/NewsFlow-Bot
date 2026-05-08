"""
Subscription model for user feed subscriptions.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from newsflow.models.base import Base

if TYPE_CHECKING:
    from newsflow.models.feed import Feed


class Subscription(Base):
    """
    User subscription to a feed.

    Links a platform channel to a feed with user preferences.
    The natural isolation is by platform + channel_id.
    """

    __tablename__ = "subscriptions"

    # Platform identification
    platform: Mapped[str] = mapped_column(String(20), nullable=False)  # discord, telegram
    platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_guild_id: Mapped[str | None] = mapped_column(String(64))  # Discord guild

    # Subscribed feed
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"))

    # User preferences
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Silent mode: don't push instant feed messages to the channel, but
    # still record SentEntry rows so the digest pipeline picks the entries
    # up. Used by channels that only want periodic AI-generated summaries.
    # The post-subscribe preview path bypasses this flag (one preview
    # always goes through so the user sees the subscription confirmed).
    silent: Mapped[bool] = mapped_column(Boolean, default=False)
    translate: Mapped[bool] = mapped_column(Boolean, default=True)
    target_language: Mapped[str] = mapped_column(String(10), default="zh-CN")

    # Display customization
    show_summary: Mapped[bool] = mapped_column(Boolean, default=True)
    show_image: Mapped[bool] = mapped_column(Boolean, default=True)

    # Filter rule: narrows which entries from this feed actually reach the
    # channel. Stored as serialized FilterRule (see core/filter.py); None
    # means no filter, all entries pass.
    filter_rule: Mapped[dict | None] = mapped_column(JSON)

    # Relationship
    feed: Mapped["Feed"] = relationship(back_populates="subscriptions")

    # Indexes
    __table_args__ = (
        # Unique subscription per channel-feed combination
        Index(
            "ix_subscriptions_channel_feed",
            "platform",
            "platform_channel_id",
            "feed_id",
            unique=True,
        ),
        # Quick lookup by platform and channel
        Index("ix_subscriptions_platform_channel", "platform", "platform_channel_id"),
        # Active subscriptions
        Index("ix_subscriptions_active", "is_active"),
    )

    def __repr__(self) -> str:
        return (
            f"<Subscription(id={self.id}, platform='{self.platform}', "
            f"channel='{self.platform_channel_id}', feed_id={self.feed_id})>"
        )


class SentEntry(Base):
    """
    Tracks which entries have been sent to which channels.

    Identified by (feed_id, guid) instead of an FK to FeedEntry.id, so
    cleanup of FeedEntry rows doesn't drop the dedupe signal. If a feed
    re-serves the same GUID after its FeedEntry has been cleaned up,
    the new ingestion creates a new FeedEntry row but the SentEntry for
    that (subscription, feed, guid) tuple still exists, so dispatch
    recognizes the article as already-seen and skips it.
    """

    __tablename__ = "sent_entries"

    # Which subscription. CASCADE so unsubscribing wipes its SentEntry.
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE")
    )

    # Which entry — natural key (feed_id + guid). No FK to feed_entries:
    # the whole point is that this row outlives FeedEntry cleanup.
    feed_id: Mapped[int] = mapped_column()
    guid: Mapped[str] = mapped_column(String(2048), nullable=False)

    # When processed (either sent or dropped by filter)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(__import__("datetime").timezone.utc),
    )

    # True if this row exists because the entry matched the subscription's
    # filter_rule out (i.e. was NOT actually delivered). Still written so the
    # dispatch loop doesn't keep re-evaluating the same entry every cycle.
    was_filtered: Mapped[bool] = mapped_column(Boolean, default=False)

    # Indexes
    __table_args__ = (
        Index(
            "ix_sent_entries_sub_feed_guid",
            "subscription_id",
            "feed_id",
            "guid",
            unique=True,
        ),
        Index("ix_sent_entries_sent_at", "sent_at"),
    )
