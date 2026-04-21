"""Tests for OPML parse/build round-trip and edge cases."""

import pytest

from newsflow.core.opml import (
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
