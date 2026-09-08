"""Platform-agnostic rendering for the subscription views: plain text with every
length bound explicit. Adapters add their own markup afterwards, so nothing here
can cut an HTML tag or a markdown link in half."""

from newsflow.core.timeutil import relative_time, time_until
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription

# Platform hard limits. Discord additionally caps the sum of every embed
# title / description / field across one message at 6000 characters.
TELEGRAM_TEXT_LIMIT = 4096
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024

# Display bounds for feed-derived text. Stored titles run to 512 characters and
# URLs to 2048, so two unclipped rows already overrun both platforms. Full URLs
# stay available via the OPML export.
TITLE_LIMIT = 80
URL_LIMIT = 200
ERROR_LIMIT = 200


def clip(text: str, limit: int) -> str:
    """Truncate to `limit` characters, marking the cut with an ellipsis."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sub_status_chip(sub: Subscription) -> str | None:
    """One-line chip when the subscription needs attention, else None.
    Priority: paused > auto-disabled > errored > silent — faults are actionable,
    silent is a deliberate choice and only worth showing when nothing else is."""
    feed = sub.feed
    if not sub.is_active:
        return "⏸ paused"
    if not feed.is_active:
        return "🛑 auto-disabled (too many errors)"
    if feed.error_count > 0:
        return f"⚠️ {feed.error_count} errors, retry {time_until(feed.next_retry_at)}"
    if sub.silent:
        return "🔇 silent (digest only)"
    return None


def sub_line_parts(sub: Subscription) -> tuple[str, str, str]:
    """(title, meta, url) for one feed-list row, each already clipped."""
    feed = sub.feed
    parts = [f"🌐 {sub.target_language}" if sub.translate else "📰 no translate"]
    chip = sub_status_chip(sub)
    if chip:
        parts.append(chip)
    return (
        clip(feed.title or "Untitled", TITLE_LIMIT),
        " · ".join(parts),
        clip(feed.url, URL_LIMIT),
    )


def sub_state(sub: Subscription, feed: Feed) -> tuple[str, str]:
    """(key, text) health state for the detail views. The key is what a platform
    maps to its own presentation (Discord picks an embed colour from it); one
    branch for both keeps the platforms agreeing on which state a sub is in."""
    if not sub.is_active:
        return "paused", "⏸ Paused"
    if not feed.is_active:
        return "disabled", "🛑 Auto-disabled (10+ consecutive errors)"
    if feed.error_count > 0:
        return "errors", f"⚠️ {feed.error_count} errors — retry {time_until(feed.next_retry_at)}"
    return "healthy", "✅ Healthy"


def last_error_text(feed: Feed) -> str | None:
    """The feed's last error, clipped — None when there is nothing to show."""
    if not feed.last_error or feed.error_count <= 0:
        return None
    return clip(feed.last_error, ERROR_LIMIT)


def recent_entry_parts(entry: FeedEntry) -> tuple[str, str | None, str]:
    """(title, link, relative time) for one recent-article row. The link is
    None when it exceeds URL_LIMIT: a truncated href points nowhere, and an
    untruncated one alone can overrun the message budget."""
    link = entry.link if entry.link and len(entry.link) <= URL_LIMIT else None
    when = relative_time(entry.published_at) if entry.published_at else ""
    return clip(entry.title, TITLE_LIMIT), link, when


def paginate_lines(
    lines: list[str],
    budget: int,
    max_items: int | None = None,
    separator: str = "\n\n",
) -> list[list[str]]:
    """Pack rendered lines into pages of at most `budget` characters. Same input
    order gives the same page boundaries, so prev/next stays stable; a line
    longer than `budget` gets a page to itself — callers bound their own lines."""
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + len(separator)
        over_budget = used + cost > budget
        over_count = max_items is not None and len(current) >= max_items
        if current and (over_budget or over_count):
            pages.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        pages.append(current)
    return pages
