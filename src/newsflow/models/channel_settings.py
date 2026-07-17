"""ChannelSettings: persisted per-channel defaults for new subscriptions.

The channel-wide commands (/settings language|translate|silent on Discord,
/language /translate /silent on Telegram) used to only bulk-update the
subscriptions that already existed — the preference itself wasn't stored
anywhere, so the next /add reverted to global defaults (translate=True,
zh-CN), and running the command on an empty channel was a no-op error.
This table records the preference; SubscriptionService.subscribe() reads
it when creating a new subscription.

NULL means "no preference recorded" — fall back to the model defaults
(and, for silent, to the legacy all-existing-subs-silent heuristic).
Per-feed overrides (/setlang, /feed language, …) still win afterwards:
inheritance happens once, at subscribe time.
"""

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from newsflow.models.base import Base


class ChannelSettings(Base):
    __tablename__ = "channel_settings"

    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)

    default_language: Mapped[str | None] = mapped_column(String(10))
    default_translate: Mapped[bool | None] = mapped_column(Boolean)
    default_silent: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        Index(
            "ix_channel_settings_platform_channel",
            "platform",
            "platform_channel_id",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelSettings(platform='{self.platform}', "
            f"channel='{self.platform_channel_id}', "
            f"lang={self.default_language!r}, translate={self.default_translate}, "
            f"silent={self.default_silent})>"
        )
