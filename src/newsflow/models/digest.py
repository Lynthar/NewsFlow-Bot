"""ChannelDigest: per-channel config for periodic AI-generated news digests.

A digest is orthogonal to feed subscriptions. A channel can have many feed
subscriptions but at most one digest config; the digest rolls up everything
the channel already received over its schedule window.

Only `schedule='weekly'` uses `delivery_weekday`; for `'daily'` it's ignored.
All times are UTC — timezone support is a later-round concern.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from newsflow.models.base import Base


class ChannelDigest(Base):
    __tablename__ = "channel_digests"

    # Channel identity
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_guild_id: Mapped[str | None] = mapped_column(String(64))

    # Schedule
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule: Mapped[str] = mapped_column(String(16), default="daily")  # daily | weekly
    delivery_hour_utc: Mapped[int] = mapped_column(Integer, default=9)
    delivery_weekday: Mapped[int | None] = mapped_column(Integer)  # 0=Mon for weekly

    # Content
    language: Mapped[str] = mapped_column(String(10), default="zh-CN")
    # When False (default), filtered-out entries don't count toward the digest.
    # When True, we include them too — useful for "I filter aggressively but
    # want to see what I've been hiding" scenarios.
    include_filtered: Mapped[bool] = mapped_column(Boolean, default=False)
    max_articles: Mapped[int] = mapped_column(Integer, default=50)

    # Delivery tracking — window for the next digest is (last_delivered_at, now]
    last_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Platform message id of the currently-pinned digest (if auto-pin is
    # enabled and the adapter supports it). Next delivery unpins this one
    # before pinning the new digest, so the channel's pin list stays at
    # "newest digest only". NULL when no digest has been pinned yet, or
    # when the adapter doesn't support pinning. Stored as a string because
    # Discord / Telegram / future platforms use different id formats.
    last_pinned_message_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        # One config per (platform, channel)
        Index(
            "ix_channel_digests_platform_channel",
            "platform",
            "platform_channel_id",
            unique=True,
        ),
        Index("ix_channel_digests_enabled", "enabled"),
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelDigest(platform='{self.platform}', "
            f"channel='{self.platform_channel_id}', "
            f"schedule='{self.schedule}')>"
        )
