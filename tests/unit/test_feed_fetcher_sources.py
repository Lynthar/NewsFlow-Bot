"""Tests for the P0 source additions in FeedFetcher / FeedService:

- JSON Feed parsing (feedparser is XML-only, so we map it ourselves),
- HTML ``<link rel="alternate">`` feed discovery (+ SSRF filtering),
- add_feed shortcut expansion and discovery-retry wiring.

All three are additive: a normal XML feed and a normal feed URL must behave
exactly as before. These tests pin the new branches without touching the
existing RSS path.
"""

import json
from datetime import UTC
from types import SimpleNamespace

import feedparser

from newsflow.core.feed_fetcher import FeedFetcher, FetchResult
from newsflow.services.feed_service import FeedService


def _f() -> FeedFetcher:
    return FeedFetcher()


# ── JSON Feed ────────────────────────────────────────────────────────────────


def test_parse_json_feed_maps_items():
    body = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "title": "My JSON Feed",
            "items": [
                {
                    "id": "a1",
                    "url": "https://ex.com/a",
                    "title": "A",
                    "content_text": "plain",
                    "date_published": "2026-05-31T10:00:00Z",
                    "authors": [{"name": "Jo"}],
                },
                {"id": "b2", "url": "https://ex.com/b", "content_html": "<p>hi</p>"},
            ],
        }
    )
    res = _f()._parse_json_feed(
        body, "application/feed+json", "https://ex.com/feed.json"
    )
    assert res is not None
    entries, title = res
    assert title == "My JSON Feed"
    assert [e["guid"] for e in entries] == ["a1", "b2"]
    assert entries[0]["title"] == "A"
    assert entries[0]["link"] == "https://ex.com/a"
    assert entries[0]["summary"] == "plain"
    assert entries[0]["author"] == "Jo"
    assert entries[0]["published_at"].tzinfo is UTC
    assert entries[1]["title"] == "Untitled"  # title-less item → fallback
    assert entries[1]["content"] == "<p>hi</p>"


def test_parse_json_feed_sniffs_without_content_type():
    # A server that mislabels the content-type still works via the sniff.
    body = (
        '{"version":"https://jsonfeed.org/version/1.1",'
        '"items":[{"id":"x","url":"https://e/x"}]}'
    )
    res = _f()._parse_json_feed(body, "text/plain", "https://e/f")
    assert res is not None
    assert res[0][0]["guid"] == "x"


def test_parse_json_feed_returns_none_for_xml():
    # An RSS/XML body must NOT be claimed by the JSON branch (caller uses
    # feedparser instead).
    xml = "<?xml version='1.0'?><rss><channel><title>x</title></channel></rss>"
    assert _f()._parse_json_feed(xml, "application/rss+xml", "https://e/f") is None


def test_parse_json_feed_idless_items_get_distinct_guids():
    body = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "items": [
                {"title": "one", "content_text": "first"},
                {"title": "two", "content_text": "second"},
            ],
        }
    )
    res = _f()._parse_json_feed(body, "application/feed+json", "https://e/f")
    assert res is not None
    entries, _ = res
    # No id and no url → content-hash guids that must differ, else dedupe would
    # collapse the two items into one.
    assert entries[0]["guid"] != entries[1]["guid"]
    assert entries[0]["link"] == "https://e/f"  # link falls back to feed url


# ── HTML feed discovery ──────────────────────────────────────────────────────


def test_discover_feeds_from_html_links():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        '<link rel="alternate" type="application/feed+json" '
        'href="https://ex.com/feed.json">'
        '<link rel="stylesheet" href="/style.css">'
        "</head><body>hi</body></html>"
    )
    out = _f()._discover_feeds(feedparser.parse(html), "https://ex.com/blog/")
    assert "https://ex.com/feed.xml" in out  # relative href resolved against base
    assert "https://ex.com/feed.json" in out
    assert len(out) == 2  # the stylesheet link is ignored


def test_discover_feeds_drops_unsafe_keeps_safe():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" '
        'href="http://169.254.169.254/feed">'
        '<link rel="alternate" type="application/rss+xml" '
        'href="https://safe.example.com/feed.xml">'
        "</head></html>"
    )
    out = _f()._discover_feeds(feedparser.parse(html), "https://ex.com/")
    # SSRF: the link-local cloud-metadata target is dropped; the safe one stays.
    assert out == ["https://safe.example.com/feed.xml"]


# ── add_feed wiring ──────────────────────────────────────────────────────────


def _ok_result(url: str) -> FetchResult:
    return FetchResult(
        url=url,
        success=True,
        feed_title="T",
        entries=[
            {
                "guid": "g1",
                "title": "T",
                "link": "https://ex.com/a",
                "summary": "",
                "content": None,
                "author": None,
                "published_at": None,
                "image_url": None,
            }
        ],
    )


async def test_add_feed_expands_shortcut(session):
    svc = FeedService(session)
    seen = {}

    async def fake_fetch(u, etag=None, last_modified=None):
        seen["url"] = u
        return _ok_result(u)

    svc.fetcher = SimpleNamespace(fetch_feed=fake_fetch)

    res = await svc.add_feed("gh:owner/repo")
    await session.commit()

    expected = "https://github.com/owner/repo/releases.atom"
    assert res.success
    assert seen["url"] == expected  # fetched the expanded URL, not the shortcut
    assert res.feed is not None and res.feed.url == expected  # stored expanded


async def test_add_feed_follows_discovered_feed(session):
    svc = FeedService(session)
    calls = []

    async def fake_fetch(u, etag=None, last_modified=None):
        calls.append(u)
        if u == "https://site.com":
            return FetchResult(
                url=u,
                success=False,
                entries=[],
                error="Parse error",
                discovered_feeds=["https://site.com/feed.xml"],
            )
        return _ok_result(u)

    svc.fetcher = SimpleNamespace(fetch_feed=fake_fetch)

    res = await svc.add_feed("https://site.com")
    await session.commit()

    assert res.success
    assert res.feed is not None
    assert res.feed.url == "https://site.com/feed.xml"  # stored the resolved URL
    assert calls == ["https://site.com", "https://site.com/feed.xml"]
