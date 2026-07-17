"""markdown_to_telegram_html: digest Markdown → Telegram-safe HTML.

Covers exactly the constructs the digest pipeline emits (bold, headings,
[text](url) links, angle-wrapped URLs, inline [N] citations) plus the
escaping guarantees that keep Telegram's strict HTML parser from
rejecting the message.
"""

from newsflow.core.telegram_markdown import markdown_to_telegram_html as conv


def test_escapes_html_specials():
    assert conv("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_bold_becomes_b_tags():
    assert conv("**Digest**") == "<b>Digest</b>"


def test_bold_does_not_span_lines():
    assert conv("**a\nb**") == "**a\nb**"


def test_heading_becomes_bold_line():
    assert conv("## Top stories") == "<b>Top stories</b>"


def test_angle_wrapped_url_unwraps_and_keeps_query_string():
    # `&` inside the URL must survive as a valid entity, not break parsing.
    assert conv("<https://ex.com/a?x=1&y=2>") == "https://ex.com/a?x=1&amp;y=2"


def test_source_list_line_round_trip():
    line = "[3] Some & Title — <https://ex.com/3?a=1&b=2>"
    assert conv(line) == "[3] Some &amp; Title — https://ex.com/3?a=1&amp;b=2"


def test_markdown_link_becomes_anchor():
    assert (
        conv("[read](https://ex.com/p?a=1&b=2)")
        == '<a href="https://ex.com/p?a=1&amp;b=2">read</a>'
    )


def test_inline_citations_left_alone():
    assert conv("Fact [1][12].") == "Fact [1][12]."


def test_full_digest_sample():
    text = "📰 **Digest**\n\nOverview [1].\n\n**来源**\n[1] T — <https://ex.com/1>"
    out = conv(text)
    assert "<b>Digest</b>" in out
    assert "<b>来源</b>" in out
    assert "[1] T — https://ex.com/1" in out
