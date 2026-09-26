"""Conditional GET: a feed fetched before is re-requested with the validators it
returned last time (RFC 9110 §13.1.1–13.1.2), and a 304 is a successful, empty
result that leaves the stored validators and the error counter untouched."""

from newsflow.repositories.feed_repository import FeedRepository
from newsflow.services.feed_service import FeedService
from tests.unit.test_feed_fetcher_redirect import _VALID_RSS, _FakeResp, _fetcher

ETAG = '"v1"'
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"
NEW_ETAG = '"v2"'
NEW_LAST_MODIFIED = "Thu, 22 Oct 2015 07:28:00 GMT"


def _sent_headers(fetcher) -> dict[str, str]:
    # HTTP field names are case-insensitive (RFC 9110 §5.1); compare them that way.
    (headers,) = fetcher._session.request_headers
    return {k.lower(): v for k, v in headers.items()}


async def test_fetch_feed_sends_stored_validators_and_accepts_304():
    url = "https://example.com/feed"
    f = _fetcher({url: _FakeResp(304, reason="Not Modified")})

    result = await f.fetch_feed(url, etag=ETAG, last_modified=LAST_MODIFIED)

    sent = _sent_headers(f)
    assert sent["if-none-match"] == ETAG
    assert sent["if-modified-since"] == LAST_MODIFIED
    assert result.success is True
    assert result.not_modified is True
    assert result.entries == []


async def _stored_feed(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    await repo.update_feed_metadata(feed.id, etag=ETAG, last_modified=LAST_MODIFIED)
    await session.refresh(feed)
    return feed


async def test_fetch_all_feeds_replays_each_feeds_validators(session):
    feed = await _stored_feed(session)
    svc = FeedService(session)
    svc.fetcher = _fetcher({feed.url: _FakeResp(304, reason="Not Modified")})

    (result,) = await svc.fetch_all_feeds()

    sent = _sent_headers(svc.fetcher)
    assert sent["if-none-match"] == ETAG
    assert sent["if-modified-since"] == LAST_MODIFIED
    assert result.success is True
    await session.refresh(feed)
    assert (feed.etag, feed.last_modified) == (ETAG, LAST_MODIFIED)
    assert feed.error_count == 0


async def test_fetch_and_store_replays_the_feeds_validators(session):
    feed = await _stored_feed(session)
    svc = FeedService(session)
    svc.fetcher = _fetcher({feed.url: _FakeResp(304, reason="Not Modified")})

    result = await svc.fetch_and_store(feed)

    sent = _sent_headers(svc.fetcher)
    assert sent["if-none-match"] == ETAG
    assert sent["if-modified-since"] == LAST_MODIFIED
    assert result.success is True
    await session.refresh(feed)
    assert (feed.etag, feed.last_modified) == (ETAG, LAST_MODIFIED)


async def test_fresh_validators_replace_the_stored_ones(session):
    """A 200 carrying new validators rotates the stored pair, so the next
    request presents the current ones rather than stale ones."""
    feed = await _stored_feed(session)
    svc = FeedService(session)
    headers = {"ETag": NEW_ETAG, "Last-Modified": NEW_LAST_MODIFIED}
    svc.fetcher = _fetcher({feed.url: _FakeResp(200, headers, body=_VALID_RSS)})

    result = await svc.fetch_and_store(feed)

    assert result.success is True
    await session.refresh(feed)
    assert (feed.etag, feed.last_modified) == (NEW_ETAG, NEW_LAST_MODIFIED)
