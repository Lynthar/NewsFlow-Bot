"""ChannelDigest: per-channel config for periodic AI-generated news digests.

A digest is orthogonal to feed subscriptions. A channel can have many feed
subscriptions but at most one digest config; the digest rolls up everything
the channel already received over its schedule window.

Only `schedule='weekly'` uses `delivery_weekday`; for `'daily'` it's ignored.
All times are UTC — timezone support is a later-round concern.
"""

from datetime import datetime, timedelta

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from newsflow.models.base import Base

# The longest window a digest reads: a weekly digest covers the 7 days before it.
WEEKLY_WINDOW = timedelta(days=7)


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
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The latest scheduled slot already served. Kept apart from last_delivered_at, so an
    # off-slot delivery or a changed hour cannot shift when the next one is due.
    last_slot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Platform message id of the currently-pinned digest; the next delivery unpins it
    # first. NULL when nothing is pinned or the adapter cannot pin. String because
    # platforms use different id formats.
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
