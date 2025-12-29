"""
Subscription model for user feed subscriptions.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
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
    translate: Mapped[bool] = mapped_column(Boolean, default=True)
    target_language: Mapped[str] = mapped_column(String(10), default="zh-CN")

    # Display customization
    show_summary: Mapped[bool] = mapped_column(Boolean, default=True)
    show_image: Mapped[bool] = mapped_column(Boolean, default=True)

    # Tracking
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_entry_guid: Mapped[str | None] = mapped_column(String(2048))

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

    This prevents duplicate sends and allows for proper cleanup.
    """

    __tablename__ = "sent_entries"

    # Which subscription
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE")
    )

    # Which entry
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("feed_entries.id", ondelete="CASCADE")
    )

    # When sent
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(__import__("datetime").timezone.utc),
    )

    # Indexes
    __table_args__ = (
        Index(
            "ix_sent_entries_subscription_entry",
            "subscription_id",
            "entry_id",
            unique=True,
        ),
        Index("ix_sent_entries_sent_at", "sent_at"),
    )
