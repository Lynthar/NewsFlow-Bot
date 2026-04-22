"""Tests for DigestService + is_due scheduling logic."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from newsflow.models.digest import ChannelDigest
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.services.digest_service import DigestService, is_due
from newsflow.services.summarization.base import DigestResult


# ===== is_due scheduling =====


def _cfg(**overrides) -> ChannelDigest:
    defaults = dict(
        platform="discord",
        platform_channel_id="c",
        platform_guild_id=None,
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        delivery_weekday=None,
        language="zh-CN",
        include_filtered=False,
        max_articles=50,
        last_delivered_at=None,
    )
    defaults.update(overrides)
    return ChannelDigest(**defaults)


def _utc(year, month, day, hour=9, minute=0, *, weekday=None):
    dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    if weekday is not None:
        assert dt.weekday() == weekday, (
            f"Test bug: {dt} has weekday {dt.weekday()}, expected {weekday}"
        )
    return dt


def test_is_due_daily_fires_at_delivery_hour():
    config = _cfg(schedule="daily", delivery_hour_utc=9)
    assert is_due(config, _utc(2026, 4, 22, 9)) is True


def test_is_due_daily_skips_other_hours():
    config = _cfg(schedule="daily", delivery_hour_utc=9)
    assert is_due(config, _utc(2026, 4, 22, 8)) is False
    assert is_due(config, _utc(2026, 4, 22, 10)) is False


def test_is_due_daily_dedupe_within_window():
    last = _utc(2026, 4, 22, 9, 0)
    config = _cfg(
        schedule="daily", delivery_hour_utc=9, last_delivered_at=last
    )
    # Loop re-checks during the same hour slot — must not double-fire.
    assert is_due(config, _utc(2026, 4, 22, 9, 30)) is False


def test_is_due_daily_fires_next_day():
    last = _utc(2026, 4, 21, 9, 0)
    config = _cfg(
        schedule="daily", delivery_hour_utc=9, last_delivered_at=last
    )
    assert is_due(config, _utc(2026, 4, 22, 9, 0)) is True


def test_is_due_weekly_requires_matching_weekday():
    # 2026-04-22 is a Wednesday (weekday=2). weekday=2 should fire.
    config = _cfg(
        schedule="weekly", delivery_hour_utc=9, delivery_weekday=2
    )
    assert is_due(config, _utc(2026, 4, 22, 9)) is True

    # A Thursday shouldn't fire.
    config2 = _cfg(
        schedule="weekly", delivery_hour_utc=9, delivery_weekday=2
    )
    assert is_due(config2, _utc(2026, 4, 23, 9)) is False


def test_is_due_respects_enabled_flag():
    config = _cfg(enabled=False, schedule="daily", delivery_hour_utc=9)
    assert is_due(config, _utc(2026, 4, 22, 9)) is False


def test_is_due_unknown_schedule_returns_false():
    config = _cfg(schedule="monthly", delivery_hour_utc=9)
    assert is_due(config, _utc(2026, 4, 22, 9)) is False


# ===== DigestService.generate =====


async def _seed_channel_with_entries(session, n_entries: int, delivered: bool = True):
    """Create a feed + subscription + N entries sent to it."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    now = datetime.now(timezone.utc)
    entries = []
    for i in range(n_entries):
        entry = FeedEntry(
            feed_id=feed.id,
            guid=f"g{i}",
            title=f"Article {i}",
            summary=f"Summary {i}",
            link=f"https://example.com/{i}",
            published_at=now - timedelta(hours=n_entries - i),
        )
        session.add(entry)
        await session.flush()
        entries.append(entry)

        if delivered:
            session.add(
                SentEntry(
                    subscription_id=sub.id,
                    entry_id=entry.id,
                    sent_at=now - timedelta(hours=n_entries - i),
                    was_filtered=False,
                )
            )
    await session.flush()
    return feed, sub, entries


async def test_digest_generate_returns_none_when_window_empty(session):
    config = ChannelDigest(
        platform="discord",
        platform_channel_id="empty-channel",
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="en",
        include_filtered=False,
        max_articles=50,
    )
    session.add(config)
    await session.flush()

    summarizer = AsyncMock()
    summarizer.generate_digest = AsyncMock(
        return_value=DigestResult(success=True, text="Should not be called")
    )
    service = DigestService(session, summarizer)

    result = await service.generate(config)

    assert result is None
    summarizer.generate_digest.assert_not_awaited()


async def test_digest_generate_invokes_summarizer_with_articles(session):
    await _seed_channel_with_entries(session, n_entries=3)

    config = ChannelDigest(
        platform="discord",
        platform_channel_id="c",
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="zh-CN",
        include_filtered=False,
        max_articles=50,
    )
    session.add(config)
    await session.flush()

    summarizer = AsyncMock()
    summarizer.generate_digest = AsyncMock(
        return_value=DigestResult(success=True, text="<digest body>")
    )
    service = DigestService(session, summarizer)

    result = await service.generate(config)

    assert result is not None
    assert result.success is True
    assert result.text == "<digest body>"

    # Inspect the call for correct shape.
    call = summarizer.generate_digest.await_args
    kwargs = call.kwargs
    assert kwargs["language"] == "zh-CN"
    assert len(kwargs["articles"]) == 3
    titles = {a.title for a in kwargs["articles"]}
    assert titles == {"Article 0", "Article 1", "Article 2"}


async def test_digest_generate_honors_max_articles_cap(session):
    await _seed_channel_with_entries(session, n_entries=5)

    config = ChannelDigest(
        platform="discord",
        platform_channel_id="c",
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="en",
        include_filtered=False,
        max_articles=2,
    )
    session.add(config)
    await session.flush()

    summarizer = AsyncMock()
    summarizer.generate_digest = AsyncMock(
        return_value=DigestResult(success=True, text="ok")
    )
    service = DigestService(session, summarizer)

    await service.generate(config)

    call = summarizer.generate_digest.await_args
    assert len(call.kwargs["articles"]) == 2


async def test_digest_generate_excludes_filtered_by_default(session):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()

    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    now = datetime.now(timezone.utc)
    # One sent, one filtered.
    sent_entry = FeedEntry(
        feed_id=feed.id, guid="real", title="Real",
        link="https://x/r", published_at=now - timedelta(hours=1),
    )
    filtered_entry = FeedEntry(
        feed_id=feed.id, guid="fil", title="Filtered",
        link="https://x/f", published_at=now - timedelta(hours=2),
    )
    session.add_all([sent_entry, filtered_entry])
    await session.flush()
    session.add_all([
        SentEntry(
            subscription_id=sub.id, entry_id=sent_entry.id,
            sent_at=now - timedelta(hours=1), was_filtered=False,
        ),
        SentEntry(
            subscription_id=sub.id, entry_id=filtered_entry.id,
            sent_at=now - timedelta(hours=2), was_filtered=True,
        ),
    ])
    await session.flush()

    config = ChannelDigest(
        platform="discord",
        platform_channel_id="c",
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="en",
        include_filtered=False,
        max_articles=50,
    )
    session.add(config)
    await session.flush()

    summarizer = AsyncMock()
    summarizer.generate_digest = AsyncMock(
        return_value=DigestResult(success=True, text="ok")
    )
    service = DigestService(session, summarizer)
    await service.generate(config)

    titles = {
        a.title for a in summarizer.generate_digest.await_args.kwargs["articles"]
    }
    assert titles == {"Real"}  # Filtered one is hidden by default


async def test_digest_generate_includes_filtered_when_configured(session):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    now = datetime.now(timezone.utc)
    a = FeedEntry(
        feed_id=feed.id, guid="a", title="A",
        link="https://x/a", published_at=now - timedelta(hours=1),
    )
    b = FeedEntry(
        feed_id=feed.id, guid="b", title="B",
        link="https://x/b", published_at=now - timedelta(hours=2),
    )
    session.add_all([a, b])
    await session.flush()
    session.add_all([
        SentEntry(
            subscription_id=sub.id, entry_id=a.id,
            sent_at=now - timedelta(hours=1), was_filtered=False,
        ),
        SentEntry(
            subscription_id=sub.id, entry_id=b.id,
            sent_at=now - timedelta(hours=2), was_filtered=True,
        ),
    ])
    await session.flush()

    config = ChannelDigest(
        platform="discord",
        platform_channel_id="c",
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="en",
        include_filtered=True,  # ← opt in
        max_articles=50,
    )
    session.add(config)
    await session.flush()

    summarizer = AsyncMock()
    summarizer.generate_digest = AsyncMock(
        return_value=DigestResult(success=True, text="ok")
    )
    service = DigestService(session, summarizer)
    await service.generate(config)

    titles = {
        a.title for a in summarizer.generate_digest.await_args.kwargs["articles"]
    }
    assert titles == {"A", "B"}
