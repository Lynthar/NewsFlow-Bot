"""Regression test for Bug 1: dispatch_once must commit feed metadata
writes even on rounds where no feed yielded new entries.

Historically the commit sat inside `if new_entries:` so 304 / empty-round
cycles silently rolled back the etag / last_modified / last_fetched_at /
error_count / next_retry_at updates — defeating the ETag cache, exponential
backoff, and the 10-errors auto-deactivate.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.services.dispatcher import Dispatcher


async def test_dispatch_once_commits_feed_metadata_when_no_new_entries(
    session, monkeypatch
):
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
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher", lambda: mock_fetcher
    )

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
