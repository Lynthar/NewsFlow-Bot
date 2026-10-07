"""What a send learns about its channel, applied to every row keyed to that channel: it moved
to a new id (Telegram group → supergroup), or it is gone for good. Delivery and digest sends
both end up here, so the two cannot drift apart again."""

from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.repositories.channel_settings_repository import ChannelSettingsRepository
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.repositories.subscription_repository import SubscriptionRepository


class Repointed(NamedTuple):
    subscriptions: int
    digests: int
    settings: int

    def __str__(self) -> str:
        return (
            f"{self.subscriptions} subscription(s), {self.digests} digest config(s) "
            f"and {self.settings} channel default(s)"
        )


async def migrate_channel(
    session: AsyncSession, platform: str, old_channel_id: str, new_channel_id: str
) -> Repointed:
    """Repoint the channel's subscriptions, digest config and defaults at `new_channel_id`.
    Rows already at the new id win over the old ones. Rewrites identity-mapped objects in
    place: read their old channel id before calling."""
    return Repointed(
        await SubscriptionRepository(session).migrate_channel(
            platform, old_channel_id, new_channel_id
        ),
        await ChannelDigestRepository(session).migrate_channel(
            platform, old_channel_id, new_channel_id
        ),
        await ChannelSettingsRepository(session).migrate_channel(
            platform, old_channel_id, new_channel_id
        ),
    )


async def retire_channel(session: AsyncSession, platform: str, channel_id: str) -> tuple[int, int]:
    """Deactivate the channel's subscriptions and disable its digest config, returning how
    many of each flipped. Idempotent. The defaults stay, for when the bot is invited back."""
    subs = await SubscriptionRepository(session).deactivate_channel(platform, channel_id)
    digests = await ChannelDigestRepository(session).disable_for_channel(platform, channel_id)
    return subs, digests
