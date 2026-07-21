"""Regression tests for the 2026-07 no-token-harness audit fixes.

Covers the render/ingest-side fixes that are unit-testable here. The webhook
breaker / SQLite write-lock fix (#1) is a cross-connection contention that the
in-memory single-session test fixture cannot reproduce; it is verified against
real on-disk SQLite by the scratchpad probe instead.
"""

from datetime import UTC, datetime, timedelta

from newsflow.adapters.base import Message
from newsflow.config import Settings
from newsflow.models.feed import Feed
from newsflow.repositories.feed_repository import FeedRepository


def _control_chars(s: str) -> list[str]:
    return [hex(ord(c)) for c in s if ord(c) < 0x20 or ord(c) == 0x7F]


# ── #2 ntfy Title header stays a single, control-char-free line ──────────────
def test_ntfy_title_header_single_line_for_cjk_newline_and_long():
    from newsflow.adapters.webhook.formats import build_payload

    for title in (
        "人工智能芯片突破：国产厂商发布新一代产品",  # CJK → base64 grows past the fold width
        "Line one\r\nX-Injected: evil\r\nLine two",  # literal CRLF injection attempt
        "A very long ascii headline that would fold " * 4,  # long → would fold
    ):
        wire = build_payload(
            "ntfy", Message(title=title, summary="s", link="https://x.test/y", source="x")
        )
        hdr = wire.headers["Title"]
        assert _control_chars(hdr) == [], f"control chars in Title for {title!r}: {hdr!r}"
        assert hdr.isascii()


# ── #3 feed-derived fields truncated to their column limits ──────────────────
async def test_create_entries_bulk_truncates_overlong_fields(session):
    feed = Feed(url="https://x.test/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    repo = FeedRepository(session)
    entries = await repo.create_entries_bulk(
        feed.id,
        [
            {
                "guid": "g" * 5000,
                "title": "T" * 5000,
                "link": "https://x.test/" + "a" * 5000,
                "author": "A" * 500,
                "image_url": "https://img.test/" + "i" * 5000,
                "summary": None,
                "content": None,
                "published_at": None,
            }
        ],
    )
    await session.commit()
    e = entries[0]
    assert len(e.title) == 1024
    assert len(e.guid) == 2048
    assert len(e.link) == 2048
    assert e.author is not None and len(e.author) == 256
    assert e.image_url is not None and len(e.image_url) == 2048


async def test_overlong_guid_dedupes_consistently(session):
    """A >2048-char guid is truncated on insert; re-serving it must be seen as
    already-existing (the existence check truncates too) — no UNIQUE collision."""
    feed = Feed(url="https://x.test/f2", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    repo = FeedRepository(session)
    data = [{"guid": "z" * 5000, "title": "T", "link": "https://x.test/1"}]
    first = await repo.create_entries_bulk(feed.id, data)
    await session.commit()
    second = await repo.create_entries_bulk(feed.id, data)
    await session.commit()
    assert len(first) == 1
    assert len(second) == 0


# ── #7 clamp only clearly-future published_at ────────────────────────────────
async def test_far_future_published_at_clamped_near_future_kept(session):
    feed = Feed(url="https://x.test/f3", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    repo = FeedRepository(session)
    now = datetime.now(UTC)
    near = now + timedelta(hours=6)  # within 1 day → untouched
    far = now + timedelta(days=400)  # clearly future → clamped to ~now
    entries = await repo.create_entries_bulk(
        feed.id,
        [
            {"guid": "near", "title": "T", "link": "https://x.test/near", "published_at": near},
            {"guid": "far", "title": "T", "link": "https://x.test/far", "published_at": far},
        ],
    )
    await session.commit()
    by_guid = {e.guid: e for e in entries}
    assert by_guid["near"].published_at == near
    assert by_guid["far"].published_at is not None
    assert by_guid["far"].published_at < now + timedelta(days=1)


# ── #5 Discord embed title is a plain field, not a markdown link ─────────────
def test_discord_embed_title_is_injection_safe():
    from newsflow.adapters.discord.bot import DiscordAdapter

    m = Message(
        title="Free stuff](https://evil.example) x",
        summary="s",
        link="https://legit.example/a",
        source="x",
    )
    embed = DiscordAdapter._create_embed(object.__new__(DiscordAdapter), m)
    assert embed.title == "Free stuff](https://evil.example) x"  # literal, not parsed
    assert embed.url == "https://legit.example/a"
    assert embed.description is None  # no markdown-link description → no injection


def test_discord_embed_title_truncated_to_256():
    from newsflow.adapters.discord.bot import DiscordAdapter

    m = Message(title="X" * 5000, summary="s", link="https://legit.example/a", source="x")
    embed = DiscordAdapter._create_embed(object.__new__(DiscordAdapter), m)
    assert embed.title is not None and len(embed.title) == 256


def test_discord_embed_url_dropped_for_non_http_link():
    from newsflow.adapters.discord.bot import DiscordAdapter

    m = Message(title="T", summary="s", link="javascript:alert(1)", source="x")
    embed = DiscordAdapter._create_embed(object.__new__(DiscordAdapter), m)
    assert embed.url is None


# ── #6 SQL echo decoupled from LOG_LEVEL ─────────────────────────────────────
def test_db_echo_defaults_off_and_independent_of_log_level():
    assert Settings(log_level="DEBUG").db_echo is False
    assert Settings(db_echo=True).db_echo is True
