"""The subscription lifecycle both declarative syncs share: create what the file
declares, keep the file's settings on the rows it owns, drop owned rows it no longer
declares. A row is owned when its platform_user_id is the sync's marker."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.subscription import Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeclaredSubscription:
    platform: str
    channel_id: str
    feed_id: int
    # Subscription columns the file sets, on creation and on every later sync.
    settings: dict[str, Any]


async def reconcile_owned_subscriptions(
    session: AsyncSession, owner: str, declared: Sequence[DeclaredSubscription], log_name: str
) -> None:
    """Make the rows marked `owner` exactly `declared`. Rows another owner holds for the
    same (platform, channel, feed) are left untouched, with a warning."""
    sub_repo = SubscriptionRepository(session)
    wanted: set[tuple[str, str, int]] = set()

    for d in declared:
        wanted.add((d.platform, d.channel_id, d.feed_id))
        route = f"{d.platform}/{d.channel_id} → feed_id={d.feed_id}"
        existing = await sub_repo.get_subscription(
            platform=d.platform, channel_id=d.channel_id, feed_id=d.feed_id
        )
        if existing is None:
            sub = Subscription(
                platform=d.platform,
                platform_user_id=owner,
                platform_channel_id=d.channel_id,
                feed_id=d.feed_id,
                is_active=True,
                **d.settings,
            )
            session.add(sub)
            await session.flush()
            # Backlog is what predates the subscription. A feed not fetched yet has none;
            # its first successful fetch seeds this row then (FeedService).
            await sub_repo.seed_predating_entries(d.feed_id, [sub.id])
            logger.info(f"{log_name}: subscribed {route}")
        elif existing.platform_user_id != owner:
            # Another owner's row must never be rewritten by this file, nor duplicated
            # into a second row that delivers everything twice.
            logger.warning(
                f"{log_name}: subscription {route} is owned by "
                f"{existing.platform_user_id!r}, not this file; leaving its settings untouched"
            )
        else:
            for column, value in d.settings.items():
                setattr(existing, column, value)
            existing.is_active = True

    # The owner filter is load-bearing: both syncs create platform="webhook" rows, and
    # without it each would delete the other's, sent history included, on every run.
    result = await session.execute(
        select(Subscription).where(Subscription.platform_user_id == owner)
    )
    for sub in result.scalars().all():
        if (sub.platform, sub.platform_channel_id, sub.feed_id) not in wanted:
            logger.info(
                f"{log_name}: unsubscribing {sub.platform}/{sub.platform_channel_id} "
                f"→ feed_id={sub.feed_id}"
            )
            await session.delete(sub)
    await session.flush()
