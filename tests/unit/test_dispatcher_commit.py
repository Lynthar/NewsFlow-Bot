"""Regression tests for dispatch_once commit semantics.

1. Feed metadata must commit even on rounds where no feed yielded new
   entries (historically the commit sat inside `if new_entries:`, so
   304 / empty rounds silently rolled back etag / backoff updates).

2. Sent-marks must commit per subscription, not once per round. The
   messages are already in users' channels the moment the adapter
   returns — a single round-end commit meant any late failure rolled
   back the WHOLE round's SentEntry rows and re-pushed every message
   on the next cycle.

The dispatcher opens its own sessions on the ``db`` fixture, so only what it
committed is visible to the assertions, exactly as in production.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher
from tests import seed


def _not_modified(*urls: str) -> MagicMock:
    """A fetcher whose every feed answers 304 with a fresh etag — the exact
    case that used to lose writes."""
    fetcher = MagicMock()
    fetcher.fetch_multiple = AsyncMock(
        return_value=[
            FetchResult(
                url=url,
                success=True,
                entries=[],
                etag='W/"fresh-etag"',
                last_modified="Wed, 22 Apr 2026 12:00:00 GMT",
                not_modified=True,
            )
            for url in urls
        ]
    )
    return fetcher


def _discord_adapter(send=None) -> MagicMock:
    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=send) if send else AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    return adapter


async def _subscribed(db, name: str) -> Subscription:
    """A Discord subscription on its own feed, with one unsent entry."""
    sub = await seed.subscription(
        db,
        platform="discord",
        channel_id=f"chan-{name}",
        user_id="u",
        url=f"https://{name}.test/rss",
        translate=False,
    )
    await seed.entry(
        db,
        sub.feed_id,
        guid=f"g-{name}",
        title="T",
        link=f"https://x.test/{name}",
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    return sub


async def test_dispatch_once_commits_feed_metadata_when_no_new_entries(db, monkeypatch):
    async with db() as session:
        session.add(Feed(url="https://example.com/feed"))
        await session.commit()
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: _not_modified("https://example.com/feed"),
    )

    result = await Dispatcher().dispatch_once()

    assert result.new_entries == 0
    assert result.errors == 0
    # The real test: metadata written by fetch_all_feeds is still there
    # after the round's session closed.
    async with db() as session:
        feed = await session.scalar(select(Feed))
    assert feed is not None and feed.last_fetched_at is not None


async def test_crash_mid_round_keeps_earlier_subscriptions_sent_marks(db, monkeypatch):
    """The first subscription delivers and commits; then the second one's
    dispatch blows up and the round aborts. The first one's SentEntry rows
    must survive the rollback — under the old whole-round transaction they
    were lost and every one of its messages was re-pushed next cycle."""
    subs = [await _subscribed(db, name) for name in ("a", "b")]
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: _not_modified(*(sub.feed.url for sub in subs)),
    )

    # Whichever subscription comes second crashes hard (outside the
    # per-entry try): its unsent-entries query explodes.
    seen: list[int] = []
    real_get_unsent = SubscriptionRepository.get_unsent_entries_for_subscription

    async def exploding_get_unsent(self, subscription_id, limit=10):
        seen.append(subscription_id)
        if subscription_id != seen[0]:
            raise RuntimeError("db hiccup")
        return await real_get_unsent(self, subscription_id, limit)

    monkeypatch.setattr(
        SubscriptionRepository, "get_unsent_entries_for_subscription", exploding_get_unsent
    )

    dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", _discord_adapter())
    result = await dispatcher.dispatch_once()

    assert result.errors == 1  # the round aborted on the second subscription
    assert result.messages_sent == 1  # the first one's entry went out

    # The first one's sent-mark was committed before the crash and survives
    # the round's rollback; the second has none.
    async with db() as session:
        marks = list(await session.scalars(select(SentEntry)))
    assert [mark.subscription_id for mark in marks] == [seen[0]]


async def test_commit_failure_for_one_subscription_does_not_abort_round(db, monkeypatch):
    """The commit recording the first message sent this round fails (SQLITE_BUSY-style);
    the recovery path must roll back and still deliver the other two in the SAME round.
    The rollback expires every cached ORM instance, so the loop must re-fetch rows
    instead of touching expired ones — doing the latter raises MissingGreenlet and
    aborts the rest of the round."""
    subs = {sub.platform_channel_id: sub for sub in [await _subscribed(db, n) for n in "abc"]}
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: _not_modified(*(sub.feed.url for sub in subs.values())),
    )

    # Raising without touching the real commit leaves the flushed mark in an open
    # transaction, which the recovery must clean up.
    real_commit = AsyncSession.commit
    fail_next_commit = False

    async def flaky_commit(self):
        nonlocal fail_next_commit
        if fail_next_commit:
            fail_next_commit = False
            raise OperationalError("stmt", None, Exception("database is locked"))
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    sent_channels: list[str] = []

    async def _send(channel_id, message):
        nonlocal fail_next_commit
        fail_next_commit = not sent_channels
        sent_channels.append(channel_id)
        return True

    dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", _discord_adapter(send=_send))
    result = await dispatcher.dispatch_once()

    # The round survived the commit failure: no round-level error, and every
    # subscription's entry actually went out on the platform.
    assert result.errors == 0
    assert sorted(sent_channels) == ["chan-a", "chan-b", "chan-c"]

    # The first subscription's mark was rolled back with the failed commit
    # (it replays next cycle); the other two were committed.
    first = subs[sent_channels[0]]
    async with db() as session:
        marks = list(await session.scalars(select(SentEntry)))
    assert sorted(mark.subscription_id for mark in marks) == sorted(
        sub.id for sub in subs.values() if sub.id != first.id
    )


async def test_failed_sent_mark_skips_only_its_own_subscription(db, monkeypatch):
    """A sent-mark whose flush fails (here: another writer committed the same key while
    the message was out) poisons the session. The round must roll back that batch and
    still deliver the subscriptions after it, with no round-level error."""
    subs = [await _subscribed(db, name) for name in "abc"]
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: _not_modified(*(sub.feed.url for sub in subs)),
    )
    first = subs[0]

    async def send(channel_id, message):
        if channel_id == first.platform_channel_id:
            async with db() as other:
                other.add(SentEntry(subscription_id=first.id, feed_id=first.feed_id, guid="g-a"))
                await other.commit()
        return True

    dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", _discord_adapter(send))
    result = await dispatcher.dispatch_once()

    assert result.errors == 0
    async with db() as session:
        marked = {mark.subscription_id for mark in await session.scalars(select(SentEntry))}
    assert marked == {sub.id for sub in subs}


async def test_a_feed_whose_entries_fail_to_store_does_not_stop_the_round(db, monkeypatch):
    """One feed's entries fail their INSERT (a trigger stands in for any data the
    database rejects). Only that feed rolls back and records the error; the other
    feed's entry is stored and delivered in the same round."""
    bad, good = [
        await seed.subscription(
            db,
            platform="discord",
            channel_id=name,
            url=f"https://{name}.test/rss",
            translate=False,
            feed_fields={"last_successful_fetch_at": datetime.now(UTC)},
        )
        for name in ("bad", "good")
    ]
    async with db() as session:
        await session.execute(
            text(
                "CREATE TRIGGER reject BEFORE INSERT ON feed_entries WHEN NEW.title = 'poison' "
                "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
            )
        )
        await session.commit()

    def fetched(sub, title: str) -> FetchResult:
        entry = {"guid": title, "title": title, "link": f"https://x.test/{title}"}
        return FetchResult(url=sub.feed.url, success=True, entries=[entry])

    fetcher = MagicMock()
    fetcher.fetch_multiple = AsyncMock(return_value=[fetched(bad, "poison"), fetched(good, "fine")])
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)
    adapter = _discord_adapter()
    dispatcher = Dispatcher()
    dispatcher.register_adapter("discord", adapter)

    result = await dispatcher.dispatch_once()

    assert result.errors == 0
    assert [call.args[0] for call in adapter.send_message.await_args_list] == ["good"]
    async with db() as session:
        bad_feed = await session.get(Feed, bad.feed_id)
    assert bad_feed is not None and bad_feed.error_count == 1
