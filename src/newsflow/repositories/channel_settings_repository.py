"""Data access for ChannelSettings (per-channel subscription defaults)."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.channel_settings import ChannelSettings

logger = logging.getLogger(__name__)


class ChannelSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, platform: str, channel_id: str) -> ChannelSettings | None:
        result = await self.session.execute(
            select(ChannelSettings).where(
                ChannelSettings.platform == platform,
                ChannelSettings.platform_channel_id == channel_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(self, platform: str, channel_id: str, **fields: Any) -> ChannelSettings:
        """Insert or update the defaults row for a channel.

        Only the fields passed are touched — `/language` must not clobber
        a silent preference recorded earlier, and vice versa.
        """
        existing = await self.get(platform, channel_id)
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            await self.session.flush()
            return existing

        row = ChannelSettings(
            platform=platform,
            platform_channel_id=channel_id,
            **fields,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row
