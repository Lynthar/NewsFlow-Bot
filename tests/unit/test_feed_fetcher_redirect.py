"""Redirect handling in FeedFetcher._do_fetch.

aiohttp's default behavior follows redirects automatically, which would let a
public (validated) feed 302 the fetcher into a private / cloud-metadata address.
The fetcher now follows redirects manually and re-validates every hop against
the SSRF allow-list. We stub aiohttp with a fake session keyed by URL so we can
assert which hosts are (and crucially are NOT) contacted.
"""

from __future__ import annotations

from newsflow.core.feed_fetcher import MAX_REDIRECTS, FeedFetcher

_VALID_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>One</title><link>https://example.com/1</link><guid>g1</guid></item>
</channel></rss>
"""


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n >= 0 else self._body


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


def _fetcher(responses: dict[str, _FakeResp]) -> FeedFetcher:
    f = FeedFetcher(max_concurrent=2)
    f._session = _FakeSession(responses)  # type: ignore[assignment]
    return f


async def test_redirect_to_private_ip_is_rejected():
    pub = "https://example.com/feed"
    f = _fetcher(
        {pub: _FakeResp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})}
    )

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
