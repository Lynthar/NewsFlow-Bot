"""DigestService.run_now — the one orchestration behind every digest.

The scheduled tick and the two /digest now handlers used to carry a copy each,
which is how the mention header once fired only on scheduled runs. These pin
the parts that differed: whether an empty window consumes the schedule slot,
where the chunk budget comes from, and what each failure reports back.

Real database and dispatcher; the platform adapter and the LLM are mocked.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.models.subscription import SentEntry
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.services.digest_service import DigestService
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


async def _delivered_article(db) -> None:
    """One article the channel received an hour ago — inside the first digest's window."""
    sub = await seed.subscription(db, platform="discord", channel_id=CHANNEL, user_id="u")
    entry = await seed.entry(db, sub.feed_id, guid="g1", title="Hello", link="https://ex.com/1")
    async with db() as session:
        session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=entry.feed_id,
                guid=entry.guid,
                sent_at=NOW - timedelta(hours=1),
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


async def _run(dispatcher, summarizer=None, *, mark_empty_delivered: bool):
    return await DigestService.run_now(
        dispatcher,
        "discord",
        CHANNEL,
        summarizer or _summarizer(DigestResult(success=True, text="body")),
        NOW,
        mark_empty_delivered=mark_empty_delivered,
    )


async def test_missing_adapter_reports_instead_of_generating(db):
    await _configured(db)
    outcome = await _run(_dispatcher(None), mark_empty_delivered=True)
    assert outcome.status == "no_adapter"
    assert await _state(db) == (None, "pin-old")


async def test_missing_config_reports_no_config(db):
    outcome = await _run(_dispatcher(_adapter()), mark_empty_delivered=True)
    assert outcome.status == "no_config"


async def test_scheduled_run_consumes_the_slot_on_an_empty_window(db):
    """is_due would re-fire the same slot on every tick until the hour
    passes, so a scheduled empty run still records a delivery."""
    await _configured(db)
    outcome = await _run(_dispatcher(_adapter()), mark_empty_delivered=True)
    assert outcome.status == "no_articles"
    assert await _state(db) == (NOW, "pin-old")


async def test_manual_run_leaves_the_slot_alone_on_an_empty_window(db):
    await _configured(db)
    outcome = await _run(_dispatcher(_adapter()), mark_empty_delivered=False)
    assert outcome.status == "no_articles"
    assert await _state(db) == (None, "pin-old")


async def test_generation_failure_carries_the_error_back(db):
    await _configured(db)
    await _delivered_article(db)
    outcome = await _run(
        _dispatcher(_adapter()),
        _summarizer(DigestResult(success=False, error="provider down")),
        mark_empty_delivered=True,
    )
    assert (outcome.status, outcome.error) == ("generation_failed", "provider down")
    assert await _state(db) == (None, "pin-old")


async def test_chunk_budget_comes_from_the_adapter_not_the_call_site(db, configure):
    configure(digest_mention_on_delivery=True)
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter(chunk_size=40)

    outcome = await _run(_dispatcher(adapter), mark_empty_delivered=True)

    first = adapter.send_digest_text_pinned.await_args.args[1]
    rest = [call.args[1] for call in adapter.send_digest_text.await_args_list]
    assert outcome.chunks == 1 + len(rest) >= 2
    assert all(len(chunk) <= 40 for chunk in [first, *rest])
    # And the header shim ran, so a manual run pings the same way a scheduled
    # one does — the drift this orchestration was introduced to stop.
    assert first.startswith("@here 📰 **Digest**")


async def test_failed_delivery_does_not_record_one(db):
    await _configured(db)
    await _delivered_article(db)
    outcome = await _run(_dispatcher(_adapter(pinned=(False, None))), mark_empty_delivered=True)
    assert outcome.status == "delivery_failed"
    assert await _state(db) == (None, "pin-old")


async def test_delivered_records_the_pin_and_reports_the_chunk_count(db):
    await _configured(db)
    await _delivered_article(db)
    adapter = _adapter()

    outcome = await _run(_dispatcher(adapter), mark_empty_delivered=True)

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
    outcome = await _run(_dispatcher(_adapter()), mark_empty_delivered=True)

    assert (outcome.status, outcome.mark_failed) == ("delivered", True)
