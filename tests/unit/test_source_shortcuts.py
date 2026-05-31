"""Tests for source URL shortcut expansion.

The critical invariant: a real feed URL (anything with "://") and any
unrecognized input must pass through UNCHANGED, so adding the shortcut layer
can never alter how an ordinary feed URL is handled.
"""

from newsflow.core.source_shortcuts import expand_source_shortcut


def test_real_urls_pass_through_unchanged():
    for u in [
        "https://example.com/feed.xml",
        "http://example.com/rss",
        "https://news.google.com/rss/search?q=already-a-url",
        "https://github.com/owner/repo/releases.atom",
    ]:
        assert expand_source_shortcut(u) == u


def test_unknown_prefix_or_plain_text_passes_through():
    assert expand_source_shortcut("foo:bar") == "foo:bar"
    assert expand_source_shortcut("just some text") == "just some text"
    assert expand_source_shortcut("") == ""


def test_news_scheme_is_not_shadowed():
    # `news:` is a real URI scheme; our Google News prefix is `gnews:`, so a
    # literal news: URI must NOT be expanded (it falls through, then validate
    # rejects it as a non-http scheme — same as today).
    assert expand_source_shortcut("news:comp.lang.python") == "news:comp.lang.python"


def test_github_shortcut():
    assert (
        expand_source_shortcut("gh:anthropics/claude-code")
        == "https://github.com/anthropics/claude-code/releases.atom"
    )


def test_gnews_shortcut_encodes_query():
    out = expand_source_shortcut("gnews:Claude AI")
    assert out.startswith("https://news.google.com/rss/search?q=Claude%20AI")
    assert "ceid=US:en" in out


def test_youtube_pypi_reddit_mastodon():
    assert (
        expand_source_shortcut("yt:UC_x5XG1OV2P6uZZ5FSM9Ttw")
        == "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"
    )
    assert (
        expand_source_shortcut("pypi:feedparser")
        == "https://pypi.org/rss/project/feedparser/releases.xml"
    )
    assert expand_source_shortcut("reddit:python") == "https://www.reddit.com/r/python.rss"
    assert expand_source_shortcut("reddit:r/python") == "https://www.reddit.com/r/python.rss"
    assert (
        expand_source_shortcut("masto:Gargron@mastodon.social")
        == "https://mastodon.social/@Gargron.rss"
    )


def test_malformed_shortcut_falls_back_to_original():
    # gh: needs owner/repo; a malformed body returns the original string, which
    # validate_feed_url then rejects — never a wrong feed.
    assert expand_source_shortcut("gh:justrepo") == "gh:justrepo"
    assert expand_source_shortcut("gnews:") == "gnews:"
    assert expand_source_shortcut("masto:nobody") == "masto:nobody"
