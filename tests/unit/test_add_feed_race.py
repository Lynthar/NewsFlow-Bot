"""Test the concurrent /add same-URL race (#8) — IntegrityError path."""

from unittest.mock import AsyncMock

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.services.feed_service import FeedService


async def test_add_feed_integrity_error_returns_existing(session):
    """If create_feed raises IntegrityError (another tx committed the same
    URL first), add_feed should roll back and reuse the winner instead of
    surfacing the SQL error to the user."""
    # Commit a feed to simulate the "winner" from another concurrent tx.
    existing = Feed(
        url="https://example.com/feed",
        title="Winner",
        is_active=True,
        error_count=0,
    )
    session.add(existing)
    await session.commit()

    svc = FeedService(session)

    # Simulate the race window: first get_feed_by_url sees nothing, so we
    # proceed to create_feed → IntegrityError → rollback → re-query.
    original_get = svc.repo.get_feed_by_url
    call_count = {"n": 0}

    async def fake_get(url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return await original_get(url)

    svc.repo.get_feed_by_url = fake_get

    svc.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="https://example.com/feed",
            success=True,
            entries=[{"guid": "a", "title": "A", "link": "https://x/a"}],
            feed_title="New Title",
        )
    )

    result = await svc.add_feed("https://example.com/feed")

    assert result.success is True
    # Returned feed is the pre-existing winner, not the one we tried to create.
    assert result.feed.url == "https://example.com/feed"
    assert result.feed.title == "Winner"
    assert "already exists" in result.message.lower()


async def test_add_feed_first_time_normal_path(session):
    """Sanity: the non-race path still works normally."""
    svc = FeedService(session)
    svc.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="https://example.com/feed",
            success=True,
            entries=[{"guid": "a", "title": "A", "link": "https://x/a"}],
            feed_title="Fresh",
        )
    )

    result = await svc.add_feed("https://example.com/feed")

    assert result.success is True
    assert result.feed.title == "Fresh"
    assert result.entry_count == 1
