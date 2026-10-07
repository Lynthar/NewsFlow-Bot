"""Tests for the ChannelMigratedError auto-migration flow.

Telegram upgrades a normal group to a supergroup when certain settings
change. Members and history survive, but the chat gets a NEW id and the
old one rejects every send with ChatMigrated from then on. Without
handling, that surfaced as an endless transient failure: entries retried
each cycle until they aged out, and the channel went silent for good.

The flow under test:
  1. TelegramAdapter recognizes ChatMigrated and raises
     ChannelMigratedError carrying the new chat id.
  2. The dispatcher catches it and repoints every subscription and any
     ChannelDigest at the new id (in place — SentEntry history stays).
  3. Delivery resumes on the next cycle against the new id.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from newsflow.adapters.base import ChannelGoneError, ChannelMigratedError
from newsflow.models.channel_settings import ChannelSettings
from newsflow.models.digest import ChannelDigest
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.channel_settings_repository import ChannelSettingsRepository
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.summarization.base import DigestResult
from tests import seed

# ===== Repository-layer tests =====


async def test_migrate_channel_repoints_subs_and_keeps_history(session):
    feed_a = Feed(url="https://a.test/rss", is_active=True, error_count=0)
    feed_b = Feed(url="https://b.test/rss", is_active=True, error_count=0)
    session.add_all([feed_a, feed_b])
    await session.flush()

    old_sub = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="-100OLD",
        feed_id=feed_a.id,
        is_active=True,
    )
    other_platform = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="-100OLD",
        feed_id=feed_a.id,
        is_active=True,
    )
    other_channel = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="-100OTHER",
        feed_id=feed_b.id,
        is_active=True,
    )
    session.add_all([old_sub, other_platform, other_channel])
    await session.flush()
    session.add(SentEntry(subscription_id=old_sub.id, feed_id=feed_a.id, guid="seen"))
    await session.commit()
    old_sub_id = old_sub.id

    repo = SubscriptionRepository(session)
    moved = await repo.migrate_channel("telegram", "-100OLD", "-100NEW")
    await session.commit()

    assert moved == 1
    # Same row, new channel id — SentEntry (keyed by subscription_id)
    # rides along untouched.
    assert old_sub.id == old_sub_id
    assert old_sub.platform_channel_id == "-100NEW"
    sent = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == old_sub_id)))
        .scalars()
        .all()
    )
    assert [s.guid for s in sent] == ["seen"]
    # A same-id channel on another platform and other telegram channels
    # are untouched.
    assert other_platform.platform_channel_id == "-100OLD"
    assert other_channel.platform_channel_id == "-100OTHER"


async def test_migrate_channel_conflict_keeps_incumbent(session):
    """If the new chat id already subscribed the same feed, the incumbent
    row wins and the old one is dropped (no UNIQUE violation, no double
    delivery)."""
    feed = Feed(url="https://a.test/rss", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    old_sub = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="-100OLD",
        feed_id=feed.id,
        is_active=True,
    )
    incumbent = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="-100NEW",
        feed_id=feed.id,
        is_active=True,
    )
    session.add_all([old_sub, incumbent])
    await session.commit()
    incumbent_id = incumbent.id

    repo = SubscriptionRepository(session)
    moved = await repo.migrate_channel("telegram", "-100OLD", "-100NEW")
    await session.commit()

    assert moved == 0
    remaining = (
        (await session.execute(select(Subscription).where(Subscription.feed_id == feed.id)))
        .scalars()
        .all()
    )
    assert [s.id for s in remaining] == [incumbent_id]


_DEFAULTS = {"default_language": "ja", "default_translate": True, "default_silent": False}


def _digest(channel_id: str, **overrides) -> ChannelDigest:
    fields = dict(
        platform="telegram",
        platform_channel_id=channel_id,
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="en",
        include_filtered=False,
        max_articles=50,
    )
    fields.update(overrides)
    return ChannelDigest(**fields)


async def test_migrate_channel_moves_digest_config(session):
    session.add(_digest("-100OLD"))
    await session.commit()

    repo = ChannelDigestRepository(session)
    moved = await repo.migrate_channel("telegram", "-100OLD", "-100NEW")
    await session.commit()

    assert moved == 1
    row = (
        await session.execute(
            select(ChannelDigest).where(ChannelDigest.platform_channel_id == "-100NEW")
        )
    ).scalar_one()
    assert row.enabled is True


async def test_migrate_channel_digest_incumbent_wins(session):
    session.add_all([_digest("-100OLD"), _digest("-100NEW", enabled=False)])
    await session.commit()

    repo = ChannelDigestRepository(session)
    moved = await repo.migrate_channel("telegram", "-100OLD", "-100NEW")
    await session.commit()

    assert moved == 0
    rows = (await session.execute(select(ChannelDigest))).scalars().all()
    assert len(rows) == 1
    assert rows[0].platform_channel_id == "-100NEW"
    assert rows[0].enabled is False  # incumbent kept as-is


async def test_migrate_channel_moves_channel_defaults(session):
    session.add(ChannelSettings(platform="telegram", platform_channel_id="-100OLD", **_DEFAULTS))
    await session.commit()

    moved = await ChannelSettingsRepository(session).migrate_channel(
        "telegram", "-100OLD", "-100NEW"
    )
    await session.commit()

    assert moved == 1
    row = await ChannelSettingsRepository(session).get("telegram", "-100NEW")
    assert row is not None and row.default_language == "ja"
    assert await ChannelSettingsRepository(session).get("telegram", "-100OLD") is None


async def test_migrate_channel_defaults_incumbent_wins(session):
    session.add_all(
        [
            ChannelSettings(platform="telegram", platform_channel_id="-100OLD", **_DEFAULTS),
            ChannelSettings(
                platform="telegram", platform_channel_id="-100NEW", default_silent=True
            ),
        ]
    )
    await session.commit()

    moved = await ChannelSettingsRepository(session).migrate_channel(
        "telegram", "-100OLD", "-100NEW"
    )
    await session.commit()

    assert moved == 0
    rows = (await session.execute(select(ChannelSettings))).scalars().all()
    assert [(r.platform_channel_id, r.default_silent) for r in rows] == [("-100NEW", True)]


async def test_migrate_channel_missing_rows_is_noop(session):
    sub_repo = SubscriptionRepository(session)
    digest_repo = ChannelDigestRepository(session)
    assert await sub_repo.migrate_channel("telegram", "nope", "-100NEW") == 0
    assert await digest_repo.migrate_channel("telegram", "nope", "-100NEW") == 0
    assert await ChannelSettingsRepository(session).migrate_channel("telegram", "nope", "x") == 0


# ===== Dispatcher-layer tests =====


async def _seed_sub_with_entry(session, *, channel_id: str) -> Subscription:
    feed = Feed(url="https://mig.test/rss", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id=channel_id,
        feed_id=feed.id,
        is_active=True,
        translate=False,
    )
    session.add(sub)
    await session.flush()
    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="g1",
            title="Title",
            link="https://mig.test/a",
            published_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await session.commit()
    return sub


async def test_dispatch_catches_migration_and_repoints(session):
    d = Dispatcher()
    sub = await _seed_sub_with_entry(session, channel_id="-100OLD")
    session.add(_digest("-100OLD"))
    session.add(ChannelSettings(platform="telegram", platform_channel_id="-100OLD", **_DEFAULTS))
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(
        side_effect=ChannelMigratedError("-100OLD", "-100NEW", reason="migrated")
    )
    adapter.is_connected = MagicMock(return_value=True)
    d._adapters["telegram"] = adapter

    sub_repo = SubscriptionRepository(session)
    dead: set[tuple[str, str]] = set()

    # Must not raise; handler repoints and tells the cycle to skip the
    # old id's remaining cached subs.
    sent = await d._dispatch_to_subscription(session, sub, sub_repo, dead_channels=dead)
    await session.commit()

    assert sent == 0
    assert ("telegram", "-100OLD") in dead

    refreshed = (
        await session.execute(select(Subscription).where(Subscription.id == sub.id))
    ).scalar_one()
    assert refreshed.platform_channel_id == "-100NEW"
    assert refreshed.is_active is True  # migrated, NOT deactivated

    digest = (await session.execute(select(ChannelDigest))).scalar_one()
    assert digest.platform_channel_id == "-100NEW"
    assert digest.enabled is True

    # A /add in the upgraded group still inherits the language set before the upgrade.
    defaults = (await session.execute(select(ChannelSettings))).scalar_one()
    assert defaults.platform_channel_id == "-100NEW"

    # The entry was never marked sent — next cycle delivers it to the
    # new chat id.
    sent_rows = (await session.execute(select(SentEntry))).scalars().all()
    assert sent_rows == []


# ===== Adapter-layer tests =====


def _tg_adapter():
    from newsflow.adapters.telegram.bot import TelegramAdapter

    adapter = TelegramAdapter(token="test-token")
    adapter.app = MagicMock()
    return adapter


async def test_telegram_send_message_raises_migrated(session):
    from telegram.error import ChatMigrated

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=ChatMigrated(-100999))

    with pytest.raises(ChannelMigratedError) as exc_info:
        await adapter.send_message("-100123", seed.message())

    assert exc_info.value.channel_id == "-100123"
    assert exc_info.value.new_channel_id == "-100999"


async def test_telegram_send_text_raises_migrated(session):
    from telegram.error import ChatMigrated

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=ChatMigrated(-100999))

    with pytest.raises(ChannelMigratedError):
        await adapter.send_text("-100123", "hello")


async def test_telegram_send_text_pinned_raises_migrated(configure):
    """The digest delivery path (send_text_pinned) must map ChatMigrated the
    same way as the ordinary send paths — otherwise a digest to a migrated
    chat falls to `return False` and retries the dead id forever."""
    from telegram.error import ChatMigrated

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=ChatMigrated(-100999))
    configure(digest_auto_pin=True)

    with pytest.raises(ChannelMigratedError) as exc_info:
        await adapter.send_text_pinned("-100123", "digest")

    assert exc_info.value.new_channel_id == "-100999"


async def test_chat_migrated_is_not_treated_as_gone():
    """ChatMigrated must map to migration, never to ChannelGone
    deactivation — the channel is alive, just renamed."""
    from telegram.error import ChatMigrated

    from newsflow.adapters.telegram.bot import TelegramAdapter

    e = ChatMigrated(-100999)
    assert TelegramAdapter._is_chat_gone(e) is False
    assert TelegramAdapter._migrated_chat_id(e) == "-100999"
    assert TelegramAdapter._migrated_chat_id(ValueError("x")) is None


def test_channel_migrated_error_carries_ids():
    e = ChannelMigratedError("-100OLD", "-100NEW", reason="upgraded")
    assert e.channel_id == "-100OLD"
    assert e.new_channel_id == "-100NEW"
    assert e.reason == "upgraded"
    assert "-100OLD" in str(e) and "-100NEW" in str(e)
    # Not a subclass of ChannelGoneError — handlers are distinct.
    assert not isinstance(e, ChannelGoneError)


async def test_scheduled_digest_that_meets_a_migration_repoints_the_channel(db, monkeypatch):
    # The digest tick has its own catch, in its own session, apart from the delivery path.
    sub = await seed.subscription(db, platform="telegram", channel_id="-100OLD")
    entry = await seed.entry(db, sub.feed_id, guid="g1")
    async with db() as session:
        session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=entry.feed_id,
                guid=entry.guid,
                sent_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.add(
            ChannelSettings(platform="telegram", platform_channel_id="-100OLD", **_DEFAULTS)
        )
        # Last served two days ago, so a catch-up is due whatever the hour is now.
        await ChannelDigestRepository(session).upsert(
            "telegram",
            "-100OLD",
            None,
            language="en",
            last_slot_at=datetime.now(UTC) - timedelta(days=2),
        )
        await session.commit()
    summarizer = MagicMock()
    summarizer.generate_digest = AsyncMock(return_value=DigestResult(success=True, text="body"))
    monkeypatch.setattr("newsflow.services.summarization.get_summarizer", lambda: summarizer)
    migrated = ChannelMigratedError("-100OLD", "-100NEW", reason="migrated")
    adapter = MagicMock(digest_chunk_size=3800)
    adapter.send_digest_text_pinned = AsyncMock(side_effect=migrated)
    adapter.send_digest_text = AsyncMock(side_effect=migrated)
    adapter.unpin_message = AsyncMock(return_value=True)
    d = Dispatcher()
    d.register_adapter("telegram", adapter)

    await d._tick_digests()

    async with db() as session:
        for model in (Subscription, ChannelDigest, ChannelSettings):
            row = (await session.execute(select(model))).scalar_one()
            assert row.platform_channel_id == "-100NEW", model.__name__
