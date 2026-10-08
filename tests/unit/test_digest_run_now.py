"""DigestService.run_now, the one orchestration behind the scheduled tick and both /digest
now handlers: whether an empty window consumes the slot, where the chunk budget comes from,
what each failure reports. Real database and dispatcher; the adapter and the LLM are mocked."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.subscription import SentEntry
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.services.digest_service import (
    DigestService,
    _most_recent_slot,
    disable_digest,
    enable_digest,
    get_digest_config,
    is_due,
)
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.summarization.base import DigestResult
from tests import seed

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
CHANNEL = "chan-1"


def _adapter(chunk_size: int = 1900, pinned=(True, "pin-new")) -> MagicMock:
    adapter = MagicMock()
    adapter.digest_chunk_size = chunk_size
    adapter.send_digest_text_pinned = AsyncMock(return_value=pinned)
    adapter.send_digest_text = AsyncMock(return_value=True)
    adapter.unpin_message = AsyncMock(return_value=True)
    return adapter


def _dispatcher(adapter: MagicMock | None) -> Dispatcher:
    dispatcher = Dispatcher()
    if adapter is not None:
        dispatcher.register_adapter("discord", adapter)
    return dispatcher


def _summarizer(result: DigestResult) -> MagicMock:
    summarizer = MagicMock()
    summarizer.generate_digest = AsyncMock(return_value=result)
    return summarizer


async def _configured(db) -> None:
    """A daily digest for the channel whose previous delivery pinned "pin-old"."""
    async with db() as session:
        await ChannelDigestRepository(session).upsert(
            "discord", CHANNEL, None, language="en", last_pinned_message_id="pin-old"
        )
        await session.commit()


async def _delivered_article(db, now: datetime = NOW) -> None:
    """One article the channel received an hour before `now` — inside the first digest's
    window."""
    sub = await seed.subscription(db, platform="discord", channel_id=CHANNEL, user_id="u")
    entry = await seed.entry(db, sub.feed_id, guid="g1", title="Hello", link="https://ex.com/1")
    async with db() as session:
        session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=entry.feed_id,
                guid=entry.guid,
                sent_at=now - timedelta(hours=1),
            )
        )
        await session.commit()


async def _state(db):
    async with db() as session:
        row = await ChannelDigestRepository(session).get("discord", CHANNEL)
    assert row is not None
    delivered = row.last_delivered_at
    if delivered is not None and delivered.tzinfo is None:  # SQLite drops tzinfo on read
        delivered = delivered.replace(tzinfo=UTC)
    return delivered, row.last_pinned_message_id


async def _run(dispatcher, summarizer=None, *, scheduled: bool):
    return await DigestService.run_now(
        dispatcher,
        "discord",
        CHANNEL,
        summarizer or _summarizer(DigestResult(success=True, text="body")),
        NOW,
        scheduled=scheduled,
    )


async def test_missing_adapter_reports_instead_of_generating(db):
    await _configured(db)
    outcome = await _run(_dispatcher(None), scheduled=True)
    assert outcome.status == "no_adapter"
    assert await _state(db) == (None, "pin-old")


async def test_missing_config_reports_no_config(db):
    outcome = await _run(_dispatcher(_adapter()), scheduled=True)
    assert outcome.status == "no_config"


async def test_scheduled_run_consumes_the_slot_on_an_empty_window(db):
    """is_due would re-fire the same slot on every tick until the hour
    passes, so a scheduled empty run still records a delivery."""
    await _configured(db)
    outcome = await _run(_dispatcher(_adapter()), scheduled=True)
    assert outcome.status == "no_articles"
    assert await _state(db) == (NOW, "pin-old")


async def test_manual_run_leaves_the_slot_alone_on_an_empty_window(db):
    await _configured(db)
    outcome = await _run(_dispatcher(_adapter()), scheduled=False)
    assert outcome.status == "no_articles"
    assert await _state(db) == (None, "pin-old")


async def test_generation_failure_carries_the_error_back(db):
    await _configured(db)
    await _delivered_article(db)
    outcome = await _run(
        _dispatcher(_adapter()),
        _summarizer(DigestResult(success=False, error="provider down")),
        scheduled=True,
    )
    assert (outcome.status, outcome.error) == ("generation_failed", "provider down")
    assert await _state(db) == (None, "pin-old")


async def test_chunk_budget_comes_from_the_adapter_not_the_call_site(db, configure):
    configure(digest_mention_on_delivery=True)
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter(chunk_size=40)

    outcome = await _run(_dispatcher(adapter), scheduled=True)

    first = adapter.send_digest_text_pinned.await_args.args[1]
    rest = [call.args[1] for call in adapter.send_digest_text.await_args_list]
    assert outcome.chunks == 1 + len(rest) >= 2
    assert all(len(chunk) <= 40 for chunk in [first, *rest])
    assert first.startswith("@here 📰 **Digest**")


async def test_manual_run_is_a_preview_that_pings_pins_and_records_nothing(db, configure):
    configure(digest_mention_on_delivery=True)
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter()

    outcome = await _run(_dispatcher(adapter), scheduled=False)

    assert outcome.status == "delivered"
    adapter.send_digest_text_pinned.assert_not_awaited()
    adapter.unpin_message.assert_not_awaited()
    assert adapter.send_digest_text.await_args.args[1].startswith("📰 **Digest**")
    assert await _state(db) == (None, "pin-old")


async def test_failed_delivery_does_not_record_one(db):
    await _configured(db)
    await _delivered_article(db)
    outcome = await _run(_dispatcher(_adapter(pinned=(False, None))), scheduled=True)
    assert outcome.status == "delivery_failed"
    assert await _state(db) == (None, "pin-old")


async def test_delivered_records_the_pin_and_reports_the_chunk_count(db):
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter()

    outcome = await _run(_dispatcher(adapter), scheduled=True)

    assert (outcome.status, outcome.chunks, outcome.mark_failed) == ("delivered", 1, False)
    assert await _state(db) == (NOW, "pin-new")
    adapter.unpin_message.assert_awaited_once_with(CHANNEL, "pin-old")


async def test_a_failed_delivery_record_is_reported_not_raised(db, monkeypatch):
    """The digest is already on-platform; raising here would lose that fact."""
    await _configured(db)
    await _delivered_article(db)

    async def locked(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(AsyncSession, "commit", locked)  # the UPDATE after delivery fails
    outcome = await _run(_dispatcher(_adapter()), scheduled=True)

    assert (outcome.status, outcome.mark_failed) == ("delivered", True)


async def test_a_slot_whose_record_failed_is_not_delivered_again(db, monkeypatch):
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter()
    dispatcher = _dispatcher(adapter)
    real_commit = AsyncSession.commit

    async def locked(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(AsyncSession, "commit", locked)
    await _run(dispatcher, scheduled=True)
    monkeypatch.setattr(AsyncSession, "commit", real_commit)

    again = await _run(dispatcher, scheduled=True)

    assert again.status == "not_due"
    assert adapter.send_digest_text_pinned.await_count == 1


async def test_a_record_that_fails_once_is_retried(db, monkeypatch):
    await _configured(db)
    await _delivered_article(db)
    real_commit = AsyncSession.commit
    failures = iter([True])

    async def flaky(self):
        if next(failures, False):
            raise RuntimeError("database is locked")
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky)
    outcome = await _run(_dispatcher(_adapter()), scheduled=True)

    assert (outcome.status, outcome.mark_failed) == ("delivered", False)
    assert await _state(db) == (NOW, "pin-new")


async def test_tick_warns_when_a_delivered_digest_could_not_be_recorded(db, monkeypatch, caplog):
    async with db() as session:
        # Last served two days ago, so a catch-up is due whatever the hour is now.
        await ChannelDigestRepository(session).upsert(
            "discord",
            CHANNEL,
            None,
            language="en",
            last_slot_at=datetime.now(UTC) - timedelta(days=2),
        )
        await session.commit()
    await _delivered_article(db, datetime.now(UTC))
    monkeypatch.setattr(
        "newsflow.services.summarization.get_summarizer",
        lambda: _summarizer(DigestResult(success=True, text="body")),
    )

    async def locked(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(AsyncSession, "commit", locked)
    await _dispatcher(_adapter())._tick_digests()

    assert f"Delivered digest to discord/{CHANNEL} (1 chunks) but could not record it" in (
        caplog.text
    )


async def test_a_failing_digest_backs_off_instead_of_retrying_every_tick(db, monkeypatch):
    async with db() as session:
        await ChannelDigestRepository(session).upsert(
            "discord", CHANNEL, None, language="en", last_slot_at=NOW - timedelta(days=2)
        )
        await session.commit()
    await _delivered_article(db)
    failed = DigestResult(success=False, error="RateLimitError")
    ok = DigestResult(success=True, text="body")
    summarizer = MagicMock()
    summarizer.generate_digest = AsyncMock(side_effect=[failed, failed, ok])
    monkeypatch.setattr("newsflow.services.summarization.get_summarizer", lambda: summarizer)
    adapter = _adapter()
    dispatcher = _dispatcher(adapter)

    # The default 5-minute check interval: the first failure holds the next run 10 minutes,
    # the second 20.
    for minutes, calls in [(0, 1), (5, 1), (10, 2), (25, 2), (30, 3)]:
        await dispatcher._tick_digests(NOW + timedelta(minutes=minutes))
        assert summarizer.generate_digest.await_count == calls, minutes

    adapter.send_digest_text_pinned.assert_awaited_once()


async def test_enabling_counts_the_current_slot_as_served(db):
    # Enabled inside its own delivery hour: without a served slot it would fire at once.
    now = datetime.now(UTC)
    async with db() as session:
        config = await enable_digest(
            session,
            "discord",
            CHANNEL,
            None,
            schedule="daily",
            local_hour=now.hour,
            local_weekday=None,
            tz=UTC,
        )
        await session.commit()

    assert is_due(config, now) is False
    next_slot = _most_recent_slot(config, now) + timedelta(days=1)
    assert is_due(config, next_slot) is True


async def test_enabling_stores_the_local_schedule_as_utc_and_keeps_unset_options(db):
    async with db() as session:
        await enable_digest(
            session,
            "discord",
            CHANNEL,
            None,
            schedule="daily",
            local_hour=21,
            local_weekday=None,
            tz=timezone(timedelta(hours=8)),
            max_articles=30,
            include_filtered=True,
        )
        config = await enable_digest(
            session,
            "discord",
            CHANNEL,
            None,
            schedule="daily",
            local_hour=9,
            local_weekday=None,
            tz=UTC,
        )
        await session.commit()

    assert config.delivery_hour_utc == 9
    assert (config.max_articles, config.include_filtered) == (30, True)


async def test_disabling_keeps_the_settings_and_reports_a_missing_digest(db):
    await _configured(db)
    async with db() as session:
        assert await disable_digest(session, "discord", "no-such-channel") is False
        assert await disable_digest(session, "discord", CHANNEL) is True
        await session.commit()
        config = await get_digest_config(session, "discord", CHANNEL)

    assert config is not None
    assert config.enabled is False
    assert config.schedule == "daily"


# ─── runs that overlap ───────────────────────────────────────────────────────


async def test_a_preview_in_flight_neither_blocks_nor_records_the_scheduled_run(db):
    """A manual preview is still waiting on the LLM when the scheduled run starts. The
    scheduled run waits for the channel, then delivers; only it pins and records."""
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter()
    dispatcher = _dispatcher(adapter)
    generating = asyncio.Event()
    release = asyncio.Event()

    async def generate_digest(**kwargs):
        if not generating.is_set():
            generating.set()
            await release.wait()
        return DigestResult(success=True, text="body")

    summarizer = MagicMock()
    summarizer.generate_digest = AsyncMock(side_effect=generate_digest)

    manual = asyncio.create_task(_run(dispatcher, summarizer, scheduled=False))
    await generating.wait()
    scheduled = asyncio.create_task(_run(dispatcher, summarizer, scheduled=True))
    await asyncio.sleep(0.05)
    release.set()
    outcomes = await asyncio.gather(manual, scheduled)

    assert [o.status for o in outcomes] == ["delivered", "delivered"]
    assert adapter.send_digest_text_pinned.await_count == 1
    assert await _state(db) == (NOW, "pin-new")


async def test_digest_waits_for_a_delivery_mark_still_being_written(db):
    """A round has flushed a sent-mark but not committed it. Reading the window then
    would miss the article, and the window would move past it for good."""
    await _configured(db)
    sub = await seed.subscription(db, platform="discord", channel_id=CHANNEL, user_id="u")
    entry = await seed.entry(db, sub.feed_id, guid="g1", title="Hello", link="https://ex.com/1")
    dispatcher = _dispatcher(_adapter())
    summarizer = _summarizer(DigestResult(success=True, text="body"))

    async with dispatcher.delivery_lock, db() as round_session:
        round_session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=entry.feed_id,
                guid=entry.guid,
                sent_at=NOW - timedelta(minutes=1),
            )
        )
        await round_session.flush()
        digest = asyncio.create_task(_run(dispatcher, summarizer, scheduled=True))
        await asyncio.sleep(0.05)
        await round_session.commit()
    outcome = await digest

    assert outcome.status == "delivered"
    articles = summarizer.generate_digest.await_args.kwargs["articles"]
    assert [a.title for a in articles] == ["Hello"]
