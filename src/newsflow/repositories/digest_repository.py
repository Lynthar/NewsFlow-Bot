"""Data access for ChannelDigest."""

import logging
from datetime import datetime
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.digest import ChannelDigest
from newsflow.models.feed import FeedEntry
from newsflow.models.subscription import SentEntry, Subscription

logger = logging.getLogger(__name__)


class ChannelDigestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, platform: str, channel_id: str
    ) -> ChannelDigest | None:
        result = await self.session.execute(
            select(ChannelDigest).where(
                ChannelDigest.platform == platform,
                ChannelDigest.platform_channel_id == channel_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        platform: str,
        channel_id: str,
        guild_id: str | None,
        **fields,
    ) -> ChannelDigest:
        """Insert or update the digest config for a channel."""
        existing = await self.get(platform, channel_id)
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            if guild_id and not existing.platform_guild_id:
                existing.platform_guild_id = guild_id
            await self.session.flush()
            return existing

        row = ChannelDigest(
            platform=platform,
            platform_channel_id=channel_id,
            platform_guild_id=guild_id,
            **fields,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def list_enabled(self) -> Sequence[ChannelDigest]:
        result = await self.session.execute(
            select(ChannelDigest).where(ChannelDigest.enabled == True)
        )
        return result.scalars().all()

    async def mark_delivered(
        self, digest_id: int, at: datetime
    ) -> None:
        await self.session.execute(
            update(ChannelDigest)
            .where(ChannelDigest.id == digest_id)
            .values(last_delivered_at=at)
        )

    async def get_channel_articles(
        self,
        platform: str,
        channel_id: str,
        since: datetime,
        until: datetime,
        *,
        include_filtered: bool,
        limit: int,
    ) -> Sequence[FeedEntry]:
        """All FeedEntries sent to this channel in (since, until]. `limit`
        caps by the *most recent* N."""
        conditions = [
            Subscription.platform == platform,
            Subscription.platform_channel_id == channel_id,
            SentEntry.sent_at > since,
            SentEntry.sent_at <= until,
        ]
        if not include_filtered:
            conditions.append(SentEntry.was_filtered == False)

        stmt = (
            select(FeedEntry)
            .join(SentEntry, SentEntry.entry_id == FeedEntry.id)
            .join(
                Subscription, Subscription.id == SentEntry.subscription_id
            )
            .where(*conditions)
            .order_by(SentEntry.sent_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        # Reverse so caller gets chronological (oldest first) in prompt.
        entries = list(result.scalars().all())
        entries.reverse()
        return entries
