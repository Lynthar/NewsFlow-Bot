"""End-to-end delivery pipeline integration test.

The focused dispatcher unit tests each pin one invariant against a
MagicMock adapter. This drives the REAL ``dispatch_once`` against a real
``BaseAdapter`` subclass that captures delivered ``Message`` objects — the
closest thing to "a subscribed channel actually receiving feed updates"
without a live Discord/Telegram connection.

It asserts the whole observable outcome a real deployment would produce:
  * the backlog is delivered oldest-first (chronological reading order),
    regardless of DB insertion order,
  * each delivered Message carries the right channel + content, and
  * a second dispatch round re-sends nothing (SentEntry dedupe) — the
    property that keeps users from getting every article twice.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.base import BaseAdapter, Message
from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.services.dispatcher import Dispatcher


class CaptureAdapter(BaseAdapter):
    """A real adapter that records deliveries instead of hitting a platform."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []

    @property
    def platform_name(self) -> str:
        return "discord"

    async def start(self) -> None:  # pragma: no cover - not exercised here
        pass

    async def stop(self) -> None:  # pragma: no cover - not exercised here
        pass

    async def send_message(self, channel_id: str, message: Message) -> bool:
        self.sent.append((channel_id, message))
        return True

    async def send_text(self, channel_id: str, text: str) -> bool:
        return True

    def is_connected(self) -> bool:
        return True


def _shared_session_factory(session):
    """A factory whose context manager yields the test session and mirrors
    the real AsyncSession's roll-back-on-exit — so only writes the code
    actually committed survive the round (dispatch commits per subscription)."""

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            await session.rollback()
            return False

    # get_session_factory() returns the factory; the caller then calls that
    # factory to get the context manager — hence the double lambda.
    return lambda: (lambda: _Ctx())


def _fake_settings() -> MagicMock:
    s = MagicMock()
    s.discord_enabled = True
    s.telegram_enabled = False
    s.webhooks_enabled = False
    s.fetch_interval_minutes = 60
    s.max_entry_publish_age_days = 0  # 0 disables the publish-age delivery guard
    s.data_dir = MagicMock()
    return s


async def test_backlog_delivers_oldest_first_then_dedupes_on_replay(session, monkeypatch):
    feed = Feed(url="https://news.test/rss", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()

    sub = Subscription(
        platform="discord",
        platform_user_id="u1",
        platform_channel_id="chan-1",
        feed_id=feed.id,
        is_active=True,
        translate=False,
    )
    session.add(sub)
    await session.flush()

    # Insert in a shuffled order to prove delivery order comes from
    # published_at (ASC), not row/insertion order.
    base = datetime.now(UTC) - timedelta(hours=6)
    entries = {
        "alpha": base + timedelta(hours=1),
        "bravo": base + timedelta(hours=2),
        "charlie": base + timedelta(hours=3),
    }
    for title in ("charlie", "alpha", "bravo"):
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=f"guid-{title}",
                title=title,
                link=f"https://news.test/{title}",
                summary=f"summary of {title}",
                published_at=entries[title],
            )
        )
    await session.commit()

    monkeypatch.setattr(
        "newsflow.services.dispatcher.get_session_factory",
        _shared_session_factory(session),
    )
    # Fetch returns no NEW entries — we are exercising backlog delivery.
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[FetchResult(url=feed.url, success=True, entries=[], not_modified=True)]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    settings = _fake_settings()
    with patch("newsflow.services.dispatcher.get_settings", return_value=settings):
        dispatcher = Dispatcher()
    adapter = CaptureAdapter()
    dispatcher._adapters["discord"] = adapter

    # --- Round 1: the whole backlog goes out, oldest-first ---
    with patch("newsflow.services.feed_service.get_settings", return_value=settings):
        round1 = await dispatcher.dispatch_once()

    assert [title for _chan, msg in adapter.sent for title in [msg.title]] == [
        "alpha",
        "bravo",
        "charlie",
    ]
    assert {chan for chan, _msg in adapter.sent} == {"chan-1"}
    assert adapter.sent[0][1].link == "https://news.test/alpha"
    assert round1.messages_sent == 3

    # --- Round 2: nothing new fetched, everything already sent -> silence ---
    adapter.sent.clear()
    with patch("newsflow.services.feed_service.get_settings", return_value=settings):
        round2 = await dispatcher.dispatch_once()

    assert adapter.sent == []  # SentEntry dedupe held; no double-delivery
    assert round2.messages_sent == 0
