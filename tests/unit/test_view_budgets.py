"""Length budgets for the subscription views on both platforms.

Feed titles are stored to 512 characters and URLs to 2048, all of it
third-party text, so two rows already overrun Telegram's 4096-character
message and Discord's 4096-character embed description. An embed over any
single field limit is rejected whole — the command then returns nothing at
all. These drive the real renderers with worst-case rows and assert the
platform limits hold. The OPML import summary is pinned here too, since both
platforms render it from the same rows.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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
    TELEGRAM_TEXT_LIMIT,
    clip,
    paginate_lines,
    recent_entry_parts,
    sub_state,
)

# The column caps in repositories/feed_repository.py, which is what a hostile
# or merely verbose feed can actually get stored.
FEED_TITLE_CAP, FEED_URL_CAP, ENTRY_URL_CAP = 512, 2048, 2048


class _SessionCtx:
    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


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


async def test_discord_feed_list_page_stays_under_the_description_limit():
    subs = [_worst_case_sub(i) for i in range(30)]
    service = MagicMock()
    service.get_channel_subscriptions = AsyncMock(return_value=subs)
    interaction = MagicMock()
    interaction.channel_id = 123
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    with (
        patch(
            "newsflow.adapters.discord.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.discord.bot.SubscriptionService", return_value=service),
    ):
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


async def test_telegram_info_message_stays_under_the_telegram_limit():
    sub = _worst_case_sub()
    detail = _detail(sub, [_worst_case_entry(i) for i in range(5)])
    service = MagicMock()
    service.get_subscription_detail = AsyncMock(return_value=detail)
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.effective_chat = MagicMock()

    with (
        patch(
            "newsflow.adapters.telegram.bot._resolve_target",
            AsyncMock(return_value=("123", ["https://ex.com/0"])),
        ),
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
    ):
        await info_command(update, MagicMock())

    text = msg.reply_text.call_args.args[0]
    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert "<b>State:</b>" in text  # the header survived the budget packing


# --- OPML import summary -----------------------------------------------------


def _import_result():
    """2 added, 1 skipped, 12 failed; the first failure overruns both display
    widths and carries markup-sensitive characters."""
    long_url = "https://ex.com/feed?" + "a&b" * 20
    long_reason = "HTTP 404: <not found> " + "x" * 90
    failed = [(long_url, long_reason)] + [(f"https://ex.com/{i}", "boom") for i in range(11)]
    return MagicMock(added=["u1", "u2"], already_subscribed=["u3"], failed=failed)


async def test_telegram_import_summary_lists_ten_failures_escaped_and_clipped():
    processing = MagicMock()
    processing.edit_text = AsyncMock()
    msg = MagicMock()
    msg.reply_text = AsyncMock(return_value=processing)
    update = MagicMock()
    update.message = msg
    service = MagicMock()
    service.import_opml = AsyncMock(return_value=_import_result())

    with (
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
    ):
        await _do_opml_import(update, chat_id="1", user_id="2", opml_content="<opml/>")

    lines = processing.edit_text.call_args.args[0].split("\n")
    assert lines[:6] == [
        "<b>OPML Import Result</b>",
        "✅ Added: <b>2</b>",
        "⏭️ Already subscribed: <b>1</b>",
        "❌ Failed: <b>12</b>",
        "",
        "<b>Failures:</b>",
    ]
    assert lines[6] == (
        "• <code>https://ex.com/feed?a&amp;ba&amp;ba&amp;ba&amp;ba&amp;ba&amp;ba&amp;ba&amp;ba&amp;b"
        "a&amp;ba&amp;ba&amp;ba&amp;ba</code>: "
        "HTTP 404: &lt;not found&gt; xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    assert sum(ln.startswith("• ") for ln in lines) == 10
    assert lines[-1] == "…and 2 more"


def test_discord_import_embed_lists_ten_failures_clipped():
    embed = _build_import_embed(_import_result())

    assert embed.title == "OPML Import Result"
    assert embed.description == "✅ Added: **2**\n⏭️ Already subscribed: **1**\n❌ Failed: **12**"
    assert [f.name for f in embed.fields] == ["Failures"]
    lines = embed.fields[0].value.split("\n")
    assert lines[0] == (
        "• `https://ex.com/feed?a&ba&ba&ba&ba&ba&ba&ba&ba&ba&ba&ba&ba&ba` — "
        "HTTP 404: <not found> xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    assert sum(ln.startswith("• ") for ln in lines) == 10
    assert lines[-1] == "…and 2 more"
