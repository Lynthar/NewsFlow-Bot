"""Markdown -> Telegram HTML for digest delivery.

Covers exactly what the digest pipeline emits: bold, ATX headings, ``[text](url)`` links
(``[[1]](url)`` for a citation) and angle-wrapped URLs. Everything else is HTML-escaped so
Telegram's strict parser cannot reject the message over a stray ``<`` or ``&``.

Not a general Markdown engine: unknown constructs pass through as escaped literal text.
"""

import re
from html import unescape

# Patterns operate on ALREADY-ESCAPED text (& < > → entities), so URL
# wrappers appear as &lt;…&gt; and ampersands inside URLs as &amp;.
_ANGLE_URL_RE = re.compile(r"&lt;(https?://\S+?)&gt;")
_MD_LINK_RE = re.compile(r"\[((?:\[[^\[\]\n]*\]|[^\[\]\n])+)\]\((https?://[^)\s\"]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_TAG_RE = re.compile(r"<[^>]*>")


def markdown_to_telegram_html(text: str) -> str:
    """Render digest Markdown as Telegram-safe HTML.

    Order matters: escape first (Telegram requires every literal &, <, >
    escaped), then unwrap ``&lt;url&gt;`` to a bare URL (Telegram
    auto-links it; previews are disabled API-side by the caller), then
    links, bold, and headings — all patterns written against the escaped
    form. ``&amp;`` inside an href is valid HTML and Telegram accepts it.
    """
    html = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = _ANGLE_URL_RE.sub(r"\1", html)
    html = _MD_LINK_RE.sub(r'<a href="\2">\1</a>', html)
    html = _BOLD_RE.sub(r"<b>\1</b>", html)
    html = _HEADING_RE.sub(r"<b>\1</b>", html)
    return html


def rendered_length(text: str) -> int:
    """The length of `text` once rendered and parsed: what Telegram counts against its
    cap, without the tags, link targets and escapes it does not count."""
    return len(unescape(_TAG_RE.sub("", markdown_to_telegram_html(text))))
