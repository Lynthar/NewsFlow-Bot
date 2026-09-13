"""Redirect handling and capped body reads in FeedFetcher.

aiohttp's default behavior follows redirects automatically, which would let a
public (validated) feed 302 the fetcher into a private / cloud-metadata address.
The fetcher now follows redirects manually and re-validates every hop against
the SSRF allow-list. We stub aiohttp with a fake session keyed by URL so we can
assert which hosts are (and crucially are NOT) contacted.

fetch_bytes_capped shares that redirect walk and is the only sanctioned way for
non-feed callers (OPML import) to read a body.
"""

from __future__ import annotations

import pytest

from newsflow.core.feed_fetcher import MAX_REDIRECTS, FeedFetcher, FeedFetchError
from newsflow.core.url_security import InvalidFeedURLError

_VALID_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>One</title><link>https://example.com/1</link><guid>g1</guid></item>
</channel></rss>
"""


# Deliberately tiny chunks. A real body arrives in pieces, and capping the read
# with read(n) would silently keep only the first one.
_CHUNK_BYTES = 16


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int = -1) -> bytes:
        # StreamReader.read(n) returns only what is already buffered, so a
        # caller that size-caps this way sees just the first chunk.
        return self._body if n < 0 else self._body[: min(n, _CHUNK_BYTES)]

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._body), _CHUNK_BYTES):
            yield self._body[i : i + _CHUNK_BYTES]


class _FakeResp:
    def __init__(
        self,
        status: int,
        headers: dict | None = None,
        body: bytes = b"",
        charset: str = "utf-8",
        reason: str = "OK",
        content_type: str = "application/xml",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.charset = charset
        self.reason = reason
        # Real aiohttp responses always expose content_type; the fetcher reads
        # it to detect JSON Feed. Default to an XML type so these redirect
        # fixtures exercise the normal feedparser path.
        self.content_type = content_type
        self.content_length = len(body) if body else None
        self.content = _FakeContent(body)

    async def __aenter__(self) -> _FakeResp:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Maps URL -> _FakeResp. Records every requested URL."""

    def __init__(self, responses: dict[str, _FakeResp]) -> None:
        self.responses = responses
        self.requested: list[str] = []
        self.closed = False

    def get(self, url: str, headers=None, allow_redirects: bool = True):
        self.requested.append(url)
        # The fix must disable aiohttp's own redirect following.
        assert allow_redirects is False
        return self.responses[url]

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await self.close()
        return False


def _fetcher(responses: dict[str, _FakeResp]) -> FeedFetcher:
    f = FeedFetcher(max_concurrent=2)
    f._session = _FakeSession(responses)  # type: ignore[assignment]
    return f


async def test_redirect_to_private_ip_is_rejected():
    pub = "https://example.com/feed"
    f = _fetcher({pub: _FakeResp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})})

    result = await f.fetch_feed(pub)

    assert result.success is False
    assert "Unsafe redirect target" in (result.error or "")
    # The private host must never have been contacted.
    assert "http://169.254.169.254/latest/meta-data/" not in f._session.requested  # type: ignore[attr-defined]


async def test_redirect_to_private_hostname_literal_rejected():
    pub = "https://example.com/feed"
    f = _fetcher({pub: _FakeResp(301, {"Location": "http://10.0.0.5/admin"})})

    result = await f.fetch_feed(pub)

    assert result.success is False
    assert "Unsafe redirect target" in (result.error or "")


async def test_redirect_to_public_is_followed():
    start = "http://example.com/feed"  # http -> https style redirect
    final = "https://example.com/feed"
    f = _fetcher(
        {
            start: _FakeResp(301, {"Location": final}),
            final: _FakeResp(200, {"ETag": '"abc"'}, body=_VALID_RSS),
        }
    )

    result = await f.fetch_feed(start)

    assert result.success is True
    assert len(result.entries) == 1
    assert result.entries[0]["guid"] == "g1"
    assert final in f._session.requested  # type: ignore[attr-defined]


async def test_relative_redirect_location_resolved():
    start = "https://example.com/old"
    f = _fetcher(
        {
            start: _FakeResp(302, {"Location": "/new"}),
            "https://example.com/new": _FakeResp(200, body=_VALID_RSS),
        }
    )

    result = await f.fetch_feed(start)

    assert result.success is True
    assert "https://example.com/new" in f._session.requested  # type: ignore[attr-defined]


async def test_too_many_redirects():
    pub = "https://example.com/loop"
    # Self-redirect forever — must bail after MAX_REDIRECTS.
    f = _fetcher({pub: _FakeResp(302, {"Location": pub})})

    result = await f.fetch_feed(pub)

    assert result.success is False
    assert "Too many redirects" in (result.error or "")
    assert len(f._session.requested) == MAX_REDIRECTS + 1  # type: ignore[attr-defined]


async def test_normal_feed_without_redirect_still_works():
    pub = "https://example.com/feed"
    f = _fetcher({pub: _FakeResp(200, {"ETag": '"v1"'}, body=_VALID_RSS)})

    result = await f.fetch_feed(pub)

    assert result.success is True
    assert result.etag == '"v1"'
    assert len(result.entries) == 1


async def test_fetch_bytes_capped_reassembles_a_chunked_body():
    # _FakeContent hands the body out in 16-byte pieces. read(n) would have
    # returned only the first one — this is the /import truncation bug.
    body = b"<opml>" + b"x" * 500 + b"</opml>"
    pub = "https://example.com/subs.opml"
    f = _fetcher({pub: _FakeResp(200, body=body, content_type="text/x-opml")})

    assert await f.fetch_bytes_capped(pub) == body


async def test_fetch_bytes_capped_returns_none_over_cap_mid_stream():
    body = b"y" * 400
    pub = "https://example.com/big.opml"
    resp = _FakeResp(200, body=body)
    resp.content_length = None  # server omits it; only the stream cap can catch this
    f = _fetcher({pub: resp})

    assert await f.fetch_bytes_capped(pub, cap=100) is None


async def test_fetch_bytes_capped_returns_none_on_declared_oversize():
    pub = "https://example.com/big.opml"
    f = _fetcher({pub: _FakeResp(200, body=b"z" * 400)})

    assert await f.fetch_bytes_capped(pub, cap=100) is None


async def test_fetch_bytes_capped_follows_and_revalidates_redirects():
    start = "https://example.com/subs"
    final = "https://example.com/subs.opml"
    f = _fetcher(
        {
            start: _FakeResp(301, {"Location": final}),
            final: _FakeResp(200, body=b"<opml/>"),
        }
    )

    assert await f.fetch_bytes_capped(start) == b"<opml/>"

    f2 = _fetcher({start: _FakeResp(302, {"Location": "http://169.254.169.254/latest/"})})
    with pytest.raises(FeedFetchError, match="Unsafe redirect target"):
        await f2.fetch_bytes_capped(start)
    assert "http://169.254.169.254/latest/" not in f2._session.requested  # type: ignore[attr-defined]


async def test_fetch_bytes_capped_rejects_unsafe_url_before_connecting():
    f = _fetcher({})

    with pytest.raises(InvalidFeedURLError):
        await f.fetch_bytes_capped("http://127.0.0.1/subs.opml")

    assert f._session.requested == []  # type: ignore[attr-defined]


async def test_fetch_bytes_capped_raises_on_http_error():
    pub = "https://example.com/subs.opml"
    f = _fetcher({pub: _FakeResp(404, reason="Not Found")})

    with pytest.raises(FeedFetchError, match="HTTP 404"):
        await f.fetch_bytes_capped(pub)
