"""Tests for DigestService + is_due scheduling logic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from newsflow.models.digest import ChannelDigest
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.services.digest_service import (
    DigestService,
    append_source_list,
    build_source_list,
    is_due,
    strip_llm_source_list,
)
from newsflow.services.summarization.base import DigestArticle, DigestResult

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
    dt = datetime(year, month, day, hour, minute, tzinfo=UTC)
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
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=last)
    # Loop re-checks during the same hour slot — must not double-fire.
    assert is_due(config, _utc(2026, 4, 22, 9, 30)) is False


def test_is_due_daily_fires_next_day():
    last = _utc(2026, 4, 21, 9, 0)
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=last)
    assert is_due(config, _utc(2026, 4, 22, 9, 0)) is True


def test_is_due_weekly_requires_matching_weekday():
    # 2026-04-22 is a Wednesday (weekday=2). weekday=2 should fire.
    config = _cfg(schedule="weekly", delivery_hour_utc=9, delivery_weekday=2)
    assert is_due(config, _utc(2026, 4, 22, 9)) is True

    # A Thursday shouldn't fire.
    config2 = _cfg(schedule="weekly", delivery_hour_utc=9, delivery_weekday=2)
    assert is_due(config2, _utc(2026, 4, 23, 9)) is False


def test_is_due_respects_enabled_flag():
    config = _cfg(enabled=False, schedule="daily", delivery_hour_utc=9)
    assert is_due(config, _utc(2026, 4, 22, 9)) is False


def test_is_due_unknown_schedule_returns_false():
    config = _cfg(schedule="monthly", delivery_hour_utc=9)
    assert is_due(config, _utc(2026, 4, 22, 9)) is False


def test_is_due_handles_naive_last_delivered_at():
    # SQLite + aiosqlite drops tzinfo on read for DateTime(timezone=True).
    # is_due() must not crash with TypeError (offset-naive vs offset-aware)
    # — that bug stalled the entire digest tick after the first delivery.
    naive = datetime(2026, 4, 21, 9, 0)  # no tzinfo
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=naive)
    assert is_due(config, _utc(2026, 4, 22, 9, 0)) is True
    assert is_due(config, _utc(2026, 4, 21, 9, 30)) is False


# ===== is_due catch-up (missed slot must deliver late, not skip) =====


def test_is_due_catches_up_after_missed_daily_slot():
    # Delivered yesterday 09:02; the process was down across today's
    # 09:00 slot. An 11:47 tick must still deliver (late) instead of
    # silently skipping to tomorrow.
    last = _utc(2026, 4, 21, 9, 2)
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=last)
    assert is_due(config, _utc(2026, 4, 22, 11, 47)) is True


def test_is_due_no_refire_after_late_catchup_delivery():
    # The catch-up delivery marked 11:50; later ticks the same day stay
    # quiet (last_delivered_at is now past the day's slot).
    last = _utc(2026, 4, 22, 11, 50)
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=last)
    assert is_due(config, _utc(2026, 4, 22, 14, 0)) is False


def test_is_due_manual_delivery_shortly_before_slot_suppresses_it():
    # /digest now at 08:00 satisfies today's 09:00 slot (dedupe delta);
    # tomorrow's slot fires normally.
    last = _utc(2026, 4, 22, 8, 0)
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=last)
    assert is_due(config, _utc(2026, 4, 22, 9, 30)) is False
    assert is_due(config, _utc(2026, 4, 23, 9, 5)) is True


def test_is_due_weekly_catches_up_within_the_week():
    # Weekly Wed 09:00 (2026-04-22 is a Wednesday). Delivered the prior
    # Wednesday; down across this Wednesday's slot → the Friday tick
    # still delivers instead of waiting a whole further week.
    last = _utc(2026, 4, 15, 9, 0, weekday=2)
    config = _cfg(
        schedule="weekly", delivery_hour_utc=9, delivery_weekday=2, last_delivered_at=last
    )
    assert is_due(config, _utc(2026, 4, 24, 16, 0)) is True


def test_is_due_first_ever_still_waits_for_the_slot_hour():
    # Enabling at 14:00 for a 09:00 slot must NOT fire immediately with a
    # surprise digest; the first delivery waits for the next slot.
    config = _cfg(schedule="daily", delivery_hour_utc=9, last_delivered_at=None)
    assert is_due(config, _utc(2026, 4, 22, 14, 0)) is False
    assert is_due(config, _utc(2026, 4, 23, 9, 0)) is True


# ===== source list assembly =====


def _articles(n: int) -> list[DigestArticle]:
    return [
        DigestArticle(
            title=f"Title {i}",
            summary="s",
            link=f"https://ex.com/{i}",
            source="ex.com",
            published_at=None,
        )
        for i in range(1, n + 1)
    ]


def test_build_source_list_lists_only_cited_articles():
    listing = build_source_list("Overview [2]. Detail [4][2].", _articles(5))
    assert listing.splitlines() == [
        "[2] Title 2 — <https://ex.com/2>",
        "[4] Title 4 — <https://ex.com/4>",
    ]


def test_build_source_list_ignores_out_of_range_citations():
    listing = build_source_list("Facts [1][7][0].", _articles(2))
    assert listing.splitlines() == ["[1] Title 1 — <https://ex.com/1>"]


def test_build_source_list_falls_back_to_all_when_nothing_cited():
    listing = build_source_list("No citations here.", _articles(3))
    assert len(listing.splitlines()) == 3


def test_build_source_list_truncates_long_titles():
    art = DigestArticle(
        title="T" * 200, summary="s", link="https://ex.com/x", source="e", published_at=None
    )
    line = build_source_list("[1]", [art]).splitlines()[0]
    assert "…" in line
    assert len(line) < 200


def test_strip_llm_source_list_removes_taught_format_tail_and_header():
    text = (
        "Body [1].\n\n**Sources**\n[1] Title — <https://ex.com/1>\n[2] Other — <https://ex.com/2>"
    )
    assert strip_llm_source_list(text) == "Body [1]."


def test_strip_llm_source_list_leaves_clean_bodies_alone():
    text = "Body [1].\n\nCross-cluster fact [2]."
    assert strip_llm_source_list(text) == text


def test_append_source_list_localizes_header_and_keeps_numbers():
    out = append_source_list("Body [2].", _articles(2), "zh-CN")
    body, _, listing = out.partition("**来源**")
    assert body.strip() == "Body [2]."
    assert listing.strip() == "[2] Title 2 — <https://ex.com/2>"


def test_append_source_list_english_header_default():
    out = append_source_list("Body [1].", _articles(1), "fr")
    assert "**Sources**" in out


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

    now = datetime.now(UTC)
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
                    feed_id=entry.feed_id,
                    guid=entry.guid,
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
    # Body is preserved; the source list is appended in code. This body
    # has no [N] citations, so the list falls back to every input
    # article, under a header localized to the digest language.
    assert result.text.startswith("<digest body>")
    assert "**来源**" in result.text
    assert "<https://example.com/0>" in result.text

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
    summarizer.generate_digest = AsyncMock(return_value=DigestResult(success=True, text="ok"))
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

    now = datetime.now(UTC)
    # One sent, one filtered.
    sent_entry = FeedEntry(
        feed_id=feed.id,
        guid="real",
        title="Real",
        link="https://x/r",
        published_at=now - timedelta(hours=1),
    )
    filtered_entry = FeedEntry(
        feed_id=feed.id,
        guid="fil",
        title="Filtered",
        link="https://x/f",
        published_at=now - timedelta(hours=2),
    )
    session.add_all([sent_entry, filtered_entry])
    await session.flush()
    session.add_all(
        [
            SentEntry(
                subscription_id=sub.id,
                feed_id=sent_entry.feed_id,
                guid=sent_entry.guid,
                sent_at=now - timedelta(hours=1),
                was_filtered=False,
            ),
            SentEntry(
                subscription_id=sub.id,
                feed_id=filtered_entry.feed_id,
                guid=filtered_entry.guid,
                sent_at=now - timedelta(hours=2),
                was_filtered=True,
            ),
        ]
    )
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
    summarizer.generate_digest = AsyncMock(return_value=DigestResult(success=True, text="ok"))
    service = DigestService(session, summarizer)
    await service.generate(config)

    titles = {a.title for a in summarizer.generate_digest.await_args.kwargs["articles"]}
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

    now = datetime.now(UTC)
    a = FeedEntry(
        feed_id=feed.id,
        guid="a",
        title="A",
        link="https://x/a",
        published_at=now - timedelta(hours=1),
    )
    b = FeedEntry(
        feed_id=feed.id,
        guid="b",
        title="B",
        link="https://x/b",
        published_at=now - timedelta(hours=2),
    )
    session.add_all([a, b])
    await session.flush()
    session.add_all(
        [
            SentEntry(
                subscription_id=sub.id,
                feed_id=a.feed_id,
                guid=a.guid,
                sent_at=now - timedelta(hours=1),
                was_filtered=False,
            ),
            SentEntry(
                subscription_id=sub.id,
                feed_id=b.feed_id,
                guid=b.guid,
                sent_at=now - timedelta(hours=2),
                was_filtered=True,
            ),
        ]
    )
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
    summarizer.generate_digest = AsyncMock(return_value=DigestResult(success=True, text="ok"))
    service = DigestService(session, summarizer)
    await service.generate(config)

    titles = {a.title for a in summarizer.generate_digest.await_args.kwargs["articles"]}
    assert titles == {"A", "B"}


async def test_digest_excludes_seeded_backlog(session):
    """Backlog seeded as already-sent on a new subscription (seeded=True) was
    never shown to the channel, so it must NOT enter the digest — even though
    its SentEntry rows are recent and was_filtered=False, and even when the
    channel opts into include_filtered. Regression for the seed/deliver
    conflation that polluted the first digest after every new subscription."""
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

    now = datetime.now(UTC)
    shown = FeedEntry(
        feed_id=feed.id,
        guid="shown",
        title="Shown",
        link="https://x/s",
        published_at=now - timedelta(hours=2),
    )
    backlog = FeedEntry(
        feed_id=feed.id,
        guid="backlog",
        title="Backlog",
        link="https://x/b",
        published_at=now - timedelta(hours=1),
    )
    session.add_all([shown, backlog])
    await session.flush()
    session.add_all(
        [
            SentEntry(
                subscription_id=sub.id,
                feed_id=shown.feed_id,
                guid=shown.guid,
                sent_at=now - timedelta(minutes=20),
                was_filtered=False,
                seeded=False,
            ),
            SentEntry(
                subscription_id=sub.id,
                feed_id=backlog.feed_id,
                guid=backlog.guid,
                sent_at=now - timedelta(minutes=10),
                was_filtered=False,
                seeded=True,
            ),
        ]
    )
    await session.flush()

    config = ChannelDigest(
        platform="discord",
        platform_channel_id="c",
        enabled=True,
        schedule="daily",
        delivery_hour_utc=9,
        language="en",
        include_filtered=True,  # even opted-in, seeded backlog stays out
        max_articles=50,
    )
    session.add(config)
    await session.flush()

    summarizer = AsyncMock()
    summarizer.generate_digest = AsyncMock(return_value=DigestResult(success=True, text="ok"))
    service = DigestService(session, summarizer)
    await service.generate(config)

    titles = {a.title for a in summarizer.generate_digest.await_args.kwargs["articles"]}
    assert titles == {"Shown"}  # seeded backlog excluded
