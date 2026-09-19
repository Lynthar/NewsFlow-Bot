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
from unittest.mock import AsyncMock, MagicMock

from newsflow.adapters.base import BaseAdapter, Message
from newsflow.core.feed_fetcher import FetchResult
from newsflow.services.dispatcher import Dispatcher
from tests import seed


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


async def test_backlog_delivers_oldest_first_then_dedupes_on_replay(db, configure, monkeypatch):
    configure(discord_token="t", max_entry_publish_age_days=0)  # 0 disables the age guard
    sub = await seed.subscription(
        db,
        platform="discord",
        channel_id="chan-1",
        user_id="u1",
        url="https://news.test/rss",
        translate=False,
    )

    # Insert in a shuffled order to prove delivery order comes from
    # published_at (ASC), not row/insertion order.
    base = datetime.now(UTC) - timedelta(hours=6)
    entries = {
        "alpha": base + timedelta(hours=1),
        "bravo": base + timedelta(hours=2),
        "charlie": base + timedelta(hours=3),
    }
    for title in ("charlie", "alpha", "bravo"):
        await seed.entry(
            db,
            sub.feed_id,
            guid=f"guid-{title}",
            title=title,
            link=f"https://news.test/{title}",
            summary=f"summary of {title}",
            published_at=entries[title],
        )

    # Fetch returns no NEW entries — we are exercising backlog delivery.
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_multiple = AsyncMock(
        return_value=[FetchResult(url=sub.feed.url, success=True, entries=[], not_modified=True)]
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher)

    dispatcher = Dispatcher()
    adapter = CaptureAdapter()
    dispatcher.register_adapter("discord", adapter)

    # --- Round 1: the whole backlog goes out, oldest-first ---
    round1 = await dispatcher.dispatch_once()

    assert [msg.title for _chan, msg in adapter.sent] == ["alpha", "bravo", "charlie"]
    assert {chan for chan, _msg in adapter.sent} == {"chan-1"}
    assert adapter.sent[0][1].link == "https://news.test/alpha"
    assert round1.messages_sent == 3

    # --- Round 2: nothing new fetched, everything already sent -> silence ---
    adapter.sent.clear()
    round2 = await dispatcher.dispatch_once()

    assert adapter.sent == []  # SentEntry dedupe held; no double-delivery
    assert round2.messages_sent == 0
