"""FeedFetcher follows redirects itself and re-validates every hop against the SSRF allow-list,
so a public feed cannot 302 it into a private address; a fake aiohttp session keyed by URL shows
which hosts are (and are not) contacted. fetch_bytes_capped, used for OPML, shares that walk.
The body that comes back is parsed as data and never opened as a URL or a file."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from multidict import CIMultiDict

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
        charset: str | None = "utf-8",
        reason: str = "OK",
        content_type: str = "application/xml",
    ) -> None:
        self.status = status
        # aiohttp exposes headers case-insensitively; so must the stand-in.
        self.headers = CIMultiDict(headers or {})
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
    """Maps URL -> _FakeResp. Records every requested URL and the headers sent with it."""

    def __init__(self, responses: dict[str, _FakeResp]) -> None:
        self.responses = responses
        self.requested: list[str] = []
        self.request_headers: list[dict[str, str]] = []
        self.closed = False

    def get(self, url: str, headers=None, allow_redirects: bool = True):
        self.requested.append(url)
        self.request_headers.append(dict(headers or {}))
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


# ── The body is data ─────────────────────────────────────────────────────────


@pytest.fixture
def loopback_feed() -> Iterator[tuple[str, list[str]]]:
    """A real feed on 127.0.0.1, which validate_feed_url refuses. Yields its URL and the
    paths it was asked for."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(_VALID_RSS)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/feed", hits
    finally:
        server.shutdown()
        server.server_close()


async def test_body_naming_a_url_is_not_fetched(loopback_feed):
    target, hits = loopback_feed
    pub = "https://example.com/feed"
    f = _fetcher({pub: _FakeResp(200, body=target.encode())})

    result = await f.fetch_feed(pub)

    assert result.entries == []
    assert hits == []


@pytest.mark.parametrize("as_uri", [False, True], ids=["path", "file-uri"])
async def test_body_naming_a_local_file_is_not_opened(tmp_path: Path, as_uri: bool):
    planted = tmp_path / "planted.xml"
    planted.write_bytes(_VALID_RSS)
    body = (planted.as_uri() if as_uri else str(planted)).encode()
    pub = "https://example.com/feed"
    f = _fetcher({pub: _FakeResp(200, body=body)})

    result = await f.fetch_feed(pub)

    assert result.entries == []


_GBK_ITEM = "<item><title>中文标题</title><link>https://example.com/1</link><guid>g1</guid></item>"


@pytest.mark.parametrize(
    ("prolog", "content_type", "charset"),
    [
        ('<?xml version="1.0" encoding="gbk"?>', None, None),
        ('<?xml version="1.0"?>', "application/rss+xml; charset=gbk", "gbk"),
    ],
    ids=["xml-declaration", "http-header"],
)
async def test_non_utf8_feed_decodes_from_its_declared_charset(
    prolog: str, content_type: str | None, charset: str | None
):
    body = f'{prolog}<rss version="2.0"><channel><title>T</title>{_GBK_ITEM}</channel></rss>'
    headers = {"Content-Type": content_type} if content_type else {}
    pub = "https://example.com/feed"
    f = _fetcher({pub: _FakeResp(200, headers, body=body.encode("gbk"), charset=charset)})

    result = await f.fetch_feed(pub)

    assert [e["title"] for e in result.entries] == ["中文标题"]
