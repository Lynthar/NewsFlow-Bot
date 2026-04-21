"""OPML 2.0 import/export for subscription lists.

OPML is the interchange format across RSS readers (Feedly, Reeder,
NetNewsWire, etc.). Import tolerates the common variants:

- nested `<outline>` categories get flattened (categories don't map to our
  per-channel subscription model)
- both `xmlUrl` and `xmlurl` attribute casings
- malformed but parseable docs — we just skip outlines without `xmlUrl`

Export always produces a flat list.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence
from xml.etree import ElementTree as ET


@dataclass
class OpmlEntry:
    url: str
    title: str | None = None
    html_url: str | None = None


class OpmlParseError(ValueError):
    """Raised when the document isn't parseable or has no feed outlines."""


def parse_opml(content: str) -> list[OpmlEntry]:
    """Extract all RSS outlines (ones with an xmlUrl attribute) from OPML."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise OpmlParseError(f"Malformed XML: {e}") from e

    entries: list[OpmlEntry] = []
    for outline in root.iter("outline"):
        url = outline.get("xmlUrl") or outline.get("xmlurl")
        if not url:
            continue
        entries.append(
            OpmlEntry(
                url=url.strip(),
                title=(outline.get("title") or outline.get("text") or "").strip() or None,
                html_url=(outline.get("htmlUrl") or outline.get("htmlurl") or "").strip() or None,
            )
        )

    if not entries:
        raise OpmlParseError("No RSS feeds found in the document")

    return entries


def build_opml(
    entries: Sequence[OpmlEntry],
    title: str = "NewsFlow Subscriptions",
) -> str:
    """Generate a flat OPML 2.0 document from the given entries."""
    opml = ET.Element("opml", version="2.0")

    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    body = ET.SubElement(opml, "body")
    for entry in entries:
        attrs = {"type": "rss", "xmlUrl": entry.url}
        if entry.title:
            attrs["text"] = entry.title
            attrs["title"] = entry.title
        if entry.html_url:
            attrs["htmlUrl"] = entry.html_url
        ET.SubElement(body, "outline", **attrs)

    ET.indent(opml, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        opml, encoding="unicode"
    )
