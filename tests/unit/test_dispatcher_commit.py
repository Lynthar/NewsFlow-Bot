"""Regression tests for dispatch_once commit semantics.

1. Feed metadata must commit even on rounds where no feed yielded new
   entries (historically the commit sat inside `if new_entries:`, so
   304 / empty rounds silently rolled back etag / backoff updates).

2. Sent-marks must commit per subscription, not once per round. The
   messages are already in users' channels the moment the adapter
   returns — a single round-end commit meant any late failure rolled
   back the WHOLE round's SentEntry rows and re-pushed every message
   on the next cycle.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher


async def test_dispatch_once_commits_feed_metadata_when_no_new_entries(session, monkeypatch):
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.commit()

    # Reuse the fixture session inside dispatch_once. Dispatcher opens the
    # session via `async with session_factory() as session:`, so we return
    # something whose __aenter__ yields our test session.
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            # Mirror the real AsyncSession context behavior: any writes not
            # already committed get rolled back on exit. Without this the
            # test passes even against the bugged code because the pending
            # UPDATE is still visible to the follow-up refresh() within the
            # same session — hiding the regression.
            await session.rollback()
            return False

    def _factory():
        return _Ctx()

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory",
        lambda: _factory,
    )

    # Mock the fetcher to return a 304 Not-Modified with a fresh etag — the
    # exact case that used to lose writes. Fresh instances make sure even
    # "update etag to something new" persists across the commit boundary.
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[
            FetchResult(
                url=feed.url,
                success=True,
                entries=[],
                etag='W/"fresh-etag"',
                last_modified="Wed, 22 Apr 2026 12:00:00 GMT",
                not_modified=True,
            )
        ]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    fake_settings = MagicMock()
    fake_settings.discord_enabled = False
    fake_settings.telegram_enabled = False
    fake_settings.webhooks_enabled = False
    fake_settings.fetch_interval_minutes = 60
    fake_settings.data_dir = MagicMock()
    with patch(
        "newsflow.services.dispatcher.get_settings",
        return_value=fake_settings,
    ):
        dispatcher = Dispatcher()

    # Same mock for feed_service's copy of get_settings.
    with patch(
        "newsflow.services.feed_service.get_settings",
        return_value=fake_settings,
    ):
        result = await dispatcher.dispatch_once()

    assert result.new_entries == 0
    assert result.errors == 0

    # The real test: metadata written by fetch_all_feeds is still there
    # after the `async with session_factory()` block exited.
    await session.refresh(feed)
    assert feed.last_fetched_at is not None


async def test_crash_mid_round_keeps_earlier_subscriptions_sent_marks(session, monkeypatch):
    """Subscription A delivers and commits; then B's dispatch blows up and
    the round aborts. A's SentEntry rows must survive the rollback — under
    the old whole-round transaction they were lost and every one of A's
    messages was re-pushed next cycle."""
    feed_a = Feed(url="https://a.test/rss", is_active=True, error_count=0)
    feed_b = Feed(url="https://b.test/rss", is_active=True, error_count=0)
    session.add_all([feed_a, feed_b])
    await session.flush()
    sub_a = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="chan-a",
        feed_id=feed_a.id,
        is_active=True,
        translate=False,
    )
    sub_b = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="chan-b",
        feed_id=feed_b.id,
        is_active=True,
        translate=False,
    )
    session.add_all([sub_a, sub_b])
    await session.flush()
    for feed in (feed_a, feed_b):
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=f"g{feed.id}",
                title="T",
                link=f"https://x.test/{feed.id}",
                published_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    await session.commit()
    # Plain-int snapshots: the round-abort rollback expires every ORM
    # instance in the session, so touching sub_a.id/sub_b.id afterwards
    # would itself raise MissingGreenlet inside this sync test code.
    sub_a_id, sub_b_id = sub_a.id, sub_b.id

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            # Mirror the real AsyncSession context: pending writes roll
            # back on exit — this is what exposes uncommitted sent-marks.
            await session.rollback()
            return False

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory",
        lambda: lambda: _Ctx(),
    )

    # No new entries this round — we're testing backlog delivery.
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[
            FetchResult(url=feed_a.url, success=True, entries=[], not_modified=True),
            FetchResult(url=feed_b.url, success=True, entries=[], not_modified=True),
        ]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    # Deterministic order: A first, then B.
    monkeypatch.setattr(
        SubscriptionRepository,
        "get_all_active_subscriptions",
        AsyncMock(return_value=[sub_a, sub_b]),
    )

    # B's dispatch crashes hard (outside the per-entry try): simulate by
    # making the unsent-entries query explode for sub_b only.
    real_get_unsent = SubscriptionRepository.get_unsent_entries_for_subscription

    async def exploding_get_unsent(self, subscription_id, limit=10):
        if subscription_id == sub_b_id:
            raise RuntimeError("db hiccup")
        return await real_get_unsent(self, subscription_id, limit)

    monkeypatch.setattr(
        SubscriptionRepository,
        "get_unsent_entries_for_subscription",
        exploding_get_unsent,
    )

    fake_settings = MagicMock()
    fake_settings.discord_enabled = False
    fake_settings.telegram_enabled = False
    fake_settings.webhooks_enabled = False
    fake_settings.fetch_interval_minutes = 60
    fake_settings.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake_settings):
        dispatcher = Dispatcher()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    dispatcher._adapters["discord"] = adapter

    with patch("newsflow.services.feed_service.get_settings", return_value=fake_settings):
        result = await dispatcher.dispatch_once()

    assert result.errors == 1  # the round aborted on B
    assert result.messages_sent == 1  # A's entry went out first

    # A's sent-mark was committed before the crash and survives the
    # round's rollback; B has none.
    marks = (await session.execute(select(SentEntry))).scalars().all()
    assert [m.subscription_id for m in marks] == [sub_a_id]


async def test_commit_failure_for_one_subscription_does_not_abort_round(session, monkeypatch):
    """The per-sub commit after subscription A fails (SQLITE_BUSY-style);
    the recovery path must roll back A's batch and still deliver B and C
    in the SAME round. The rollback expires every cached ORM instance, so
    the loop must re-fetch rows instead of touching expired ones — doing
    the latter raises MissingGreenlet and aborts the rest of the round."""
    feeds, subs = [], []
    for name in ("a", "b", "c"):
        feed = Feed(url=f"https://{name}.test/rss", is_active=True, error_count=0)
        session.add(feed)
        await session.flush()
        sub = Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id=f"chan-{name}",
            feed_id=feed.id,
            is_active=True,
            translate=False,
        )
        session.add(sub)
        await session.flush()
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=f"g-{name}",
                title="T",
                link=f"https://x.test/{name}",
                published_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        feeds.append(feed)
        subs.append(sub)
    await session.commit()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            await session.rollback()
            return False

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory",
        lambda: lambda: _Ctx(),
    )

    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[
            FetchResult(url=f.url, success=True, entries=[], not_modified=True) for f in feeds
        ]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    # Deterministic order: A, B, C.
    monkeypatch.setattr(
        SubscriptionRepository,
        "get_all_active_subscriptions",
        AsyncMock(return_value=list(subs)),
    )

    # The FIRST per-subscription commit (after A) fails; later ones succeed.
    # Raising without touching the real commit leaves the transaction open
    # with A's flushed marks — exactly the state the recovery rollback has
    # to clean up.
    real_commit = type(session).commit
    tripped = {"done": False}

    async def flaky_commit(self):
        if self is session and not tripped["done"]:
            tripped["done"] = True
            raise OperationalError("stmt", None, Exception("database is locked"))
        return await real_commit(self)

    monkeypatch.setattr(type(session), "commit", flaky_commit)

    fake_settings = MagicMock()
    fake_settings.discord_enabled = False
    fake_settings.telegram_enabled = False
    fake_settings.webhooks_enabled = False
    fake_settings.fetch_interval_minutes = 60
    fake_settings.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake_settings):
        dispatcher = Dispatcher()

    sent_channels: list[str] = []

    async def _send(channel_id, message):
        sent_channels.append(channel_id)
        return True

    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=_send)
    adapter.is_connected = MagicMock(return_value=True)
    dispatcher._adapters["discord"] = adapter

    with patch("newsflow.services.feed_service.get_settings", return_value=fake_settings):
        result = await dispatcher.dispatch_once()

    # The round survived the commit failure: no round-level error, and every
    # subscription's entry actually went out on the platform.
    assert result.errors == 0
    assert sent_channels == ["chan-a", "chan-b", "chan-c"]

    # A's mark was rolled back with the failed commit (bounded replay next
    # cycle); B's and C's were committed by their own per-sub commits.
    marks = (await session.execute(select(SentEntry))).scalars().all()
    assert sorted(m.subscription_id for m in marks) == [subs[1].id, subs[2].id]
