"""Entries a platform keeps refusing are given up on, but only in a round where a notice
to the same channel goes through: a channel that refuses everything is down, and its
entries must survive until it recovers. Filtered and silenced entries spend no sends."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select, update

from newsflow.adapters.base import Message, UndeliverableError
from newsflow.core.feed_fetcher import FetchResult
from newsflow.core.filter import FilterRule
from newsflow.models.feed import FeedEntry
from newsflow.models.subscription import SentEntry
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.services.dispatcher import SENDS_PER_ROUND, STRIKES_TO_GIVE_UP, Dispatcher
from tests import seed

URL = "https://example.com/feed"


async def _subscription_with_entries(db, monkeypatch, titles, **fields):
    sub = await seed.subscription(
        db,
        platform="discord",
        channel_id="c",
        url=URL,
        title="Example",
        translate=False,
        **fields,
    )
    base = datetime.now(UTC) - timedelta(hours=2)
    for i, title in enumerate(titles):
        await seed.entry(
            db,
            sub.feed_id,
            guid=f"g{i}",
            title=title,
            link=f"https://example.com/{i}",
            published_at=base + timedelta(minutes=i),
        )
    fetcher = MagicMock()
    fetcher.fetch_multiple = AsyncMock(
        return_value=[FetchResult(url=URL, success=True, entries=[], not_modified=True)]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)
    return sub


def _dispatcher(send_message, *, notice_ok=True):
    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=send_message)
    adapter.send_text = AsyncMock(return_value=notice_ok)
    adapter.is_connected = MagicMock(return_value=True)
    d = Dispatcher()
    d.register_adapter("discord", adapter)
    return d, adapter


async def _rows(db, sub_id):
    async with db() as session:
        return list(
            await session.scalars(select(SentEntry).where(SentEntry.subscription_id == sub_id))
        )


async def test_refused_entry_is_given_up_after_its_strikes_and_never_starves_the_rest(
    db, monkeypatch
):
    sub = await _subscription_with_entries(db, monkeypatch, [f"T{i}" for i in range(5)])

    async def send(_channel, message: Message) -> bool:
        return message.title != "T0"

    d, adapter = _dispatcher(send)

    first = await d.dispatch_once()
    assert first.messages_sent == 4
    for _ in range(STRIKES_TO_GIVE_UP - 1):
        await d.dispatch_once()

    adapter.send_text.assert_awaited_once()
    assert '1 article from "Example"' in adapter.send_text.await_args.args[1]
    rows = {row.guid: row for row in await _rows(db, sub.id)}
    assert rows["g0"].undeliverable is True
    assert not any(rows[f"g{i}"].undeliverable for i in range(1, 5))
    assert d.totals.entries_undeliverable == 1

    await d.dispatch_once()
    attempts_at_t0 = [c for c in adapter.send_message.await_args_list if c.args[1].title == "T0"]
    assert len(attempts_at_t0) == STRIKES_TO_GIVE_UP


async def test_a_channel_that_refuses_everything_loses_nothing(db, monkeypatch):
    sub = await _subscription_with_entries(db, monkeypatch, ["A", "B", "C"])
    healthy = False

    async def send(_channel, _message) -> bool:
        return healthy

    d, adapter = _dispatcher(send, notice_ok=False)

    for _ in range(STRIKES_TO_GIVE_UP + 2):
        await d.dispatch_once()
    assert await _rows(db, sub.id) == []
    assert adapter.send_text.await_count == 3

    healthy = True
    result = await d.dispatch_once()

    assert result.messages_sent == 3
    assert not any(row.undeliverable for row in await _rows(db, sub.id))


async def test_undeliverable_error_gives_up_in_the_same_round_once_the_notice_lands(
    db, monkeypatch
):
    sub = await _subscription_with_entries(db, monkeypatch, ["A"])

    async def send(channel, _message) -> bool:
        raise UndeliverableError(channel, reason="HTTP 400")

    d, _adapter = _dispatcher(send)
    await d.dispatch_once()

    assert [(row.guid, row.undeliverable) for row in await _rows(db, sub.id)] == [("g0", True)]


async def test_undeliverable_error_stays_queued_while_the_notice_is_refused(db, monkeypatch):
    sub = await _subscription_with_entries(db, monkeypatch, ["A"])
    refused = True

    async def send(channel, _message) -> bool:
        if refused:
            raise UndeliverableError(channel, reason="HTTP 400")
        return True

    d, adapter = _dispatcher(send, notice_ok=False)
    await d.dispatch_once()
    assert await _rows(db, sub.id) == []

    refused = False
    await d.dispatch_once()

    assert [(row.guid, row.undeliverable) for row in await _rows(db, sub.id)] == [("g0", False)]


async def test_digest_leaves_out_undeliverable_entries(db, monkeypatch):
    await _subscription_with_entries(db, monkeypatch, ["Refused", "Posted"])

    async def send(channel, message: Message) -> bool:
        if message.title == "Refused":
            raise UndeliverableError(channel)
        return True

    d, _adapter = _dispatcher(send)
    await d.dispatch_once()

    async with db() as session:
        articles = await ChannelDigestRepository(session).get_channel_articles(
            "discord",
            "c",
            datetime.now(UTC) - timedelta(days=1),
            datetime.now(UTC) + timedelta(minutes=1),
            include_filtered=True,
            limit=50,
        )
    assert [a.title for a in articles] == ["Posted"]


async def test_filtered_entries_do_not_spend_the_round_budget(db, monkeypatch):
    titles = [f"skip {i}" for i in range(50)] + [f"keep {i}" for i in range(15)]
    rule = FilterRule(include_keywords=("keep",)).to_json()
    sub = await _subscription_with_entries(db, monkeypatch, titles, filter_rule=rule)

    async def send(_channel, _message) -> bool:
        return True

    d, adapter = _dispatcher(send)
    result = await d.dispatch_once()

    assert result.messages_sent == SENDS_PER_ROUND
    rows = await _rows(db, sub.id)
    assert sum(row.was_filtered for row in rows) == 50
    assert [c.args[1].title for c in adapter.send_message.await_args_list] == [
        f"keep {i}" for i in range(SENDS_PER_ROUND)
    ]


async def test_silent_subscription_clears_its_backlog_in_one_round(db, monkeypatch):
    sub = await _subscription_with_entries(
        db, monkeypatch, [f"T{i}" for i in range(30)], silent=True
    )

    async def send(_channel, _message) -> bool:
        return True

    d, adapter = _dispatcher(send)
    await d.dispatch_once()

    assert len(await _rows(db, sub.id)) == 30
    adapter.send_message.assert_not_awaited()


async def test_cleanup_counts_the_queued_entries_it_deletes(db, monkeypatch, caplog):
    sub = await _subscription_with_entries(db, monkeypatch, ["queued", "sent"])
    paused = await seed.subscription(
        db, platform="discord", channel_id="p", url=URL, translate=False, is_active=False
    )
    async with db() as session:
        await session.execute(
            update(FeedEntry).values(created_at=datetime.now(UTC) - timedelta(days=30))
        )
        session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=sub.feed_id,
                guid="g1",
                sent_at=datetime.now(UTC) - timedelta(days=20),
            )
        )
        await session.commit()

    d = Dispatcher()
    await d.cleanup_once()

    assert (await seed.subscription_row(db, sub.id)).dropped_unsent == 1
    assert (await seed.subscription_row(db, paused.id)).dropped_unsent == 0
    assert d.totals.entries_dropped_unsent == 1
    assert f"Subscription {sub.id} (discord/c): cleanup deleted 1 entries" in caplog.text
