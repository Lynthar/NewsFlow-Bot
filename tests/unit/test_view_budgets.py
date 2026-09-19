"""Length budgets for the subscription views on both platforms.

Feed titles are stored to 512 characters and URLs to 2048, all of it
third-party text, so two rows already overrun Telegram's 4096-character
message and Discord's 4096-character embed description. An embed over any
single field limit is rejected whole — the command then returns nothing at
all. These drive the real renderers with worst-case rows and assert the
platform limits hold. The OPML import summary is pinned here too, since both
platforms render it from the same rows.
"""

import html
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from newsflow.adapters.discord.bot import (
    FeedCommands,
    _build_import_embed,
    _build_status_embed,
    _format_sub_line,
)
from newsflow.adapters.telegram.bot import _do_opml_import, info_command
from newsflow.adapters.views import (
    DISCORD_EMBED_DESCRIPTION_LIMIT,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_TITLE_LIMIT,
    IMPORT_REASON_LIMIT,
    IMPORT_URL_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    clip,
    paginate_lines,
    recent_entry_parts,
    sub_state,
)
from newsflow.core.feed_fetcher import FetchResult
from newsflow.services.subscription_service import OpmlImportResult
from tests import seed

# The column caps in repositories/feed_repository.py, which is what a hostile
# or merely verbose feed can actually get stored.
FEED_TITLE_CAP, FEED_URL_CAP, ENTRY_URL_CAP = 512, 2048, 2048


def _worst_case_sub(i: int = 0):
    feed = MagicMock()
    feed.title = "T&" * (FEED_TITLE_CAP // 2)
    feed.url = f"https://ex.com/{i}?" + "&a=1" * ((FEED_URL_CAP - 20) // 4)
    feed.description = "D" * 900
    feed.is_active = True
    feed.error_count = 3
    feed.next_retry_at = None
    feed.last_error = "E&" * 400
    feed.last_successful_fetch_at = None
    feed.last_fetched_at = None
    sub = MagicMock()
    sub.feed = feed
    sub.is_active = True
    sub.silent = False
    sub.translate = True
    sub.target_language = "zh-CN"
    return sub


WORST_TITLE = "T&" * (FEED_TITLE_CAP // 2)
WORST_ERROR = "E&" * 400


def _worst_url(i: int) -> str:
    return f"https://ex.com/{i}?" + "&a=1" * ((FEED_URL_CAP - 20) // 4)


async def _worst_case_row(db, *, platform: str, channel_id: str, i: int = 0):
    """The widest subscription the columns admit, stored for real."""
    return await seed.subscription(
        db,
        platform=platform,
        channel_id=channel_id,
        url=_worst_url(i),
        title=WORST_TITLE,
        feed_fields={"description": "D" * 900, "error_count": 3, "last_error": WORST_ERROR},
        translate=True,
        target_language="zh-CN",
    )


def _worst_case_entry(i: int = 0):
    """A link just short enough to survive clipping, so the row is kept and
    counts against the message budget — the expensive case, not the dropped one."""
    entry = MagicMock()
    entry.title = "A&" * 300
    entry.link = f"https://ex.com/{i}?" + "&b=2" * 45
    entry.published_at = None
    return entry


def _over_long_entry():
    entry = _worst_case_entry()
    entry.link = "https://ex.com/x?" + "&b=2" * ((ENTRY_URL_CAP - 20) // 4)
    return entry


def _detail(sub, entries):
    return MagicMock(subscription=sub, feed=sub.feed, recent_entries=entries, unsent_count=7)


# --- shared view helpers -----------------------------------------------------


def test_clip_result_never_exceeds_the_limit():
    assert clip("abc", 10) == "abc"
    assert len(clip("x" * 100, 10)) == 10
    assert clip("x" * 100, 10).endswith("…")


def test_paginate_lines_packs_by_budget_and_keeps_every_line():
    lines = [f"line {i} " + "x" * 40 for i in range(20)]
    pages = paginate_lines(lines, budget=200)
    assert [ln for page in pages for ln in page] == lines
    for page in pages[:-1]:
        assert len("\n\n".join(page)) <= 200


def test_paginate_lines_honours_the_item_cap_as_well():
    pages = paginate_lines(["a"] * 10, budget=10_000, max_items=3)
    assert [len(p) for p in pages] == [3, 3, 3, 1]


def test_recent_entry_parts_drops_an_over_long_link():
    """A truncated href points nowhere, so an over-long link is dropped
    rather than cut — the title still renders, just without the anchor."""
    assert recent_entry_parts(_over_long_entry())[1] is None
    short = _worst_case_entry()
    short.link = "https://ex.com/a"
    assert recent_entry_parts(short)[1] == "https://ex.com/a"


def test_sub_state_key_and_text_come_from_one_branch():
    sub = _worst_case_sub()
    sub.is_active = False
    assert sub_state(sub, sub.feed)[0] == "paused"
    sub.is_active = True
    sub.feed.is_active = False
    assert sub_state(sub, sub.feed)[0] == "disabled"
    sub.feed.is_active = True
    assert sub_state(sub, sub.feed)[0] == "errors"
    sub.feed.error_count = 0
    assert sub_state(sub, sub.feed) == ("healthy", "✅ Healthy")


# --- Discord /feed list ------------------------------------------------------


async def test_discord_feed_list_page_stays_under_the_description_limit(db):
    for i in range(30):
        await _worst_case_row(db, platform="discord", channel_id="123", i=i)
    interaction = MagicMock()
    interaction.channel_id = 123
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await FeedCommands.feed_list.callback(FeedCommands(MagicMock()), interaction, 1)

    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert len(embed.description) <= DISCORD_EMBED_DESCRIPTION_LIMIT
    # And the rows are still there — a budget that silently emptied the page
    # would pass a length assertion too.
    assert embed.description.count("**") >= 2


def test_discord_sub_line_is_bounded_even_at_the_column_caps():
    assert len(_format_sub_line(_worst_case_sub())) < 400


# --- Discord /feed status ----------------------------------------------------


def test_discord_status_embed_respects_every_field_limit():
    embed = _build_status_embed(
        _detail(_worst_case_sub(), [_worst_case_entry(i) for i in range(5)])
    )
    assert len(embed.title) <= DISCORD_EMBED_TITLE_LIMIT
    assert len(embed.description) <= 300
    for field in embed.fields:
        assert len(field.value) <= DISCORD_EMBED_FIELD_VALUE_LIMIT
    total = len(embed.title) + len(embed.description)
    total += sum(len(f.name) + len(f.value) for f in embed.fields)
    assert total <= 6000  # Discord's message-wide allowance across all embeds


# --- Telegram /info ----------------------------------------------------------


async def test_telegram_info_message_stays_under_the_telegram_limit(db):
    sub = await _worst_case_row(db, platform="telegram", channel_id="123")
    for i in range(5):
        entry = _worst_case_entry(i)
        await seed.entry(db, sub.feed_id, guid=f"g{i}", title=entry.title, link=entry.link)
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.effective_chat = SimpleNamespace(id=123, type="private")
    context = MagicMock()
    context.args = [sub.feed.url]

    await info_command(update, context)

    text = msg.reply_text.call_args.args[0]
    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert "<b>State:</b>" in text  # the header survived the budget packing


# --- OPML import summary -----------------------------------------------------


LONG_URL = "https://ex.com/feed?" + "a&b" * 20
LONG_REASON = "Failed to fetch feed: HTTP 404: <not found> " + "x" * 90


def _import_result() -> OpmlImportResult:
    """2 added, 1 skipped, 12 failed; the first failure overruns both display
    widths and carries markup-sensitive characters."""
    failed = [(LONG_URL, LONG_REASON)] + [(f"https://ex.com/{i}", "boom") for i in range(11)]
    return OpmlImportResult(added=["u1", "u2"], already_subscribed=["u3"], failed=failed)


def _opml(urls: list[str]) -> str:
    outlines = "".join(f'<outline type="rss" xmlUrl="{html.escape(u)}"/>' for u in urls)
    return f"<opml version='2.0'><body>{outlines}</body></opml>"


async def test_telegram_import_summary_lists_ten_failures_escaped_and_clipped(db, monkeypatch):
    """The same 2 / 1 / 12 outcome as _import_result, produced by a real import:
    two new feeds fetch fine, one is already subscribed, twelve fail to fetch."""
    added = ["https://ok.example/1", "https://ok.example/2"]
    failed = [LONG_URL] + [f"https://ex.com/{i}" for i in range(11)]
    await seed.subscription(db, channel_id="1", url="https://ok.example/3")

    async def fetch_feed(url: str) -> FetchResult:
        if url in added:
            entry = {"guid": url, "title": "E", "link": url}
            return FetchResult(url=url, success=True, entries=[entry], feed_title="Fine")
        error = LONG_REASON.removeprefix("Failed to fetch feed: ") if url == LONG_URL else "boom"
        return FetchResult(url=url, success=False, entries=[], error=error)

    fetcher = MagicMock()
    fetcher.fetch_feed = AsyncMock(side_effect=fetch_feed)
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)
    processing = MagicMock()
    processing.edit_text = AsyncMock()
    msg = MagicMock()
    msg.reply_text = AsyncMock(return_value=processing)
    msg.is_topic_message = False
    update = MagicMock()
    update.message = msg

    await _do_opml_import(
        update,
        chat_id="1",
        user_id="2",
        opml_content=_opml(added + ["https://ok.example/3"] + failed),
    )

    lines = processing.edit_text.call_args.args[0].split("\n")
    assert lines[:6] == [
        "<b>OPML Import Result</b>",
        "✅ Added: <b>2</b>",
        "⏭️ Already subscribed: <b>1</b>",
        "❌ Failed: <b>12</b>",
        "",
        "<b>Failures:</b>",
    ]

    def escape(text: str) -> str:
        return html.escape(text, quote=False)

    assert lines[6] == (
        f"• <code>{escape(LONG_URL[:IMPORT_URL_LIMIT])}</code>: "
        f"{escape(LONG_REASON[:IMPORT_REASON_LIMIT])}"
    )
    assert "<not found>" not in lines[6]
    assert sum(ln.startswith("• ") for ln in lines) == 10
    assert lines[-1] == "…and 2 more"


def test_discord_import_embed_lists_ten_failures_clipped():
    embed = _build_import_embed(_import_result())

    assert embed.title == "OPML Import Result"
    assert embed.description == "✅ Added: **2**\n⏭️ Already subscribed: **1**\n❌ Failed: **12**"
    assert [f.name for f in embed.fields] == ["Failures"]
    lines = embed.fields[0].value.split("\n")
    assert lines[0] == (f"• `{LONG_URL[:IMPORT_URL_LIMIT]}` — {LONG_REASON[:IMPORT_REASON_LIMIT]}")
    assert sum(ln.startswith("• ") for ln in lines) == 10
    assert lines[-1] == "…and 2 more"
