"""Tests for OPML parse/build round-trip, edge cases and the upload size cap."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from newsflow.adapters.telegram.bot import import_document
from newsflow.core.opml import (
    MAX_OPML_BYTES,
    OpmlEntry,
    OpmlParseError,
    build_opml,
    parse_opml,
)

SAMPLE_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>My Feeds</title></head>
  <body>
    <outline type="rss" text="BBC" title="BBC News"
             xmlUrl="https://feeds.bbci.co.uk/news/rss.xml"
             htmlUrl="https://bbc.com"/>
    <outline type="rss" text="HN"
             xmlUrl="https://hnrss.org/frontpage"/>
  </body>
</opml>
"""

NESTED_OPML = """<?xml version="1.0"?>
<opml version="2.0">
  <body>
    <outline text="News">
      <outline type="rss" text="BBC"
               xmlUrl="https://bbc.co.uk/rss"/>
    </outline>
    <outline text="Tech">
      <outline type="rss" text="HN"
               xmlUrl="https://hnrss.org/frontpage"/>
      <outline type="rss" text="TC"
               xmlUrl="https://techcrunch.com/feed/"/>
    </outline>
  </body>
</opml>
"""


def test_parse_opml_flat():
    entries = parse_opml(SAMPLE_OPML)

    assert len(entries) == 2
    assert entries[0].url == "https://feeds.bbci.co.uk/news/rss.xml"
    assert entries[0].title == "BBC News"  # prefers title over text
    assert entries[0].html_url == "https://bbc.com"
    assert entries[1].url == "https://hnrss.org/frontpage"
    assert entries[1].title == "HN"  # falls back to text when no title
    assert entries[1].html_url is None


def test_parse_opml_flattens_nested_categories():
    entries = parse_opml(NESTED_OPML)

    assert {e.url for e in entries} == {
        "https://bbc.co.uk/rss",
        "https://hnrss.org/frontpage",
        "https://techcrunch.com/feed/",
    }


def test_parse_opml_ignores_outlines_without_xmlurl():
    doc = """<opml><body>
        <outline text="Category without URL"/>
        <outline type="rss" xmlUrl="https://example.com/feed"/>
    </body></opml>"""
    entries = parse_opml(doc)

    assert len(entries) == 1
    assert entries[0].url == "https://example.com/feed"


def test_parse_opml_rejects_document_with_no_feeds():
    doc = """<opml><body><outline text="empty"/></body></opml>"""
    with pytest.raises(OpmlParseError, match="No RSS feeds"):
        parse_opml(doc)


def test_parse_opml_rejects_malformed_xml():
    with pytest.raises(OpmlParseError, match="Malformed"):
        parse_opml("<opml><body>unclosed")


def test_parse_opml_accepts_lowercase_xmlurl():
    """Some exporters use lowercase xmlurl — be tolerant."""
    doc = """<opml><body>
        <outline type="rss" xmlurl="https://example.com/feed" text="Test"/>
    </body></opml>"""
    entries = parse_opml(doc)
    assert len(entries) == 1
    assert entries[0].url == "https://example.com/feed"


def test_build_opml_round_trip():
    originals = [
        OpmlEntry(
            url="https://feeds.bbci.co.uk/news/rss.xml",
            title="BBC News",
            html_url="https://bbc.com",
        ),
        OpmlEntry(url="https://hnrss.org/frontpage", title="HN"),
    ]

    xml = build_opml(originals)
    parsed = parse_opml(xml)

    assert [e.url for e in parsed] == [e.url for e in originals]
    assert [e.title for e in parsed] == [e.title for e in originals]


def test_build_opml_starts_with_declaration():
    xml = build_opml([OpmlEntry(url="https://example.com/feed")])
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert 'version="2.0"' in xml
    assert 'xmlUrl="https://example.com/feed"' in xml


def test_build_opml_includes_creation_date():
    xml = build_opml([OpmlEntry(url="https://example.com/feed")])
    assert "<dateCreated>" in xml


def test_import_rejects_over_feed_ceiling():
    """Each imported feed is fetched serially (~30s worst case); an
    unbounded file would turn one /import into an hours-long handler."""
    from newsflow.core.opml import MAX_OPML_FEEDS

    outlines = "".join(
        f'<outline type="rss" xmlUrl="https://example.com/f{i}"/>'
        for i in range(MAX_OPML_FEEDS + 1)
    )
    doc = f'<opml version="2.0"><body>{outlines}</body></opml>'
    with pytest.raises(OpmlParseError, match="Too many feeds"):
        parse_opml(doc)


def test_import_at_feed_ceiling_is_accepted():
    from newsflow.core.opml import MAX_OPML_FEEDS

    outlines = "".join(
        f'<outline type="rss" xmlUrl="https://example.com/f{i}"/>' for i in range(MAX_OPML_FEEDS)
    )
    doc = f'<opml version="2.0"><body>{outlines}</body></opml>'
    assert len(parse_opml(doc)) == MAX_OPML_FEEDS


async def test_an_oversized_upload_is_refused_before_it_is_downloaded():
    update = MagicMock()
    update.effective_chat.type = "private"
    update.message.reply_text = AsyncMock()
    update.message.document.file_name = "feeds.opml"
    update.message.document.file_size = MAX_OPML_BYTES + 1
    update.message.document.get_file = AsyncMock()

    await import_document(update, MagicMock())

    assert "too large (1 MB cap)" in update.message.reply_text.call_args.args[0]
    update.message.document.get_file.assert_not_awaited()
