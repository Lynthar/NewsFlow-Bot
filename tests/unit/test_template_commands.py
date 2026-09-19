"""/template (Telegram) and /feed template (Discord) command surfaces.

Drives the real handlers against the real database and pins: show/set/
reset/all forms, multiline + \\n input normalization, set-time placeholder
validation, the conditional admin gate (bare show stays open, mutations are
gated), and the preview reply.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from newsflow.adapters.discord.bot import FeedCommands
from newsflow.adapters.telegram.bot import template_command
from tests import seed

URL = "https://ex.com/feed"


async def _template_of(db, sub_id: int) -> str | None:
    row = await seed.subscription_row(db, sub_id)
    assert row is not None
    return row.message_template


# ---------------------------------------------------------------- telegram

CHAT = "777"


async def _tg_sub(db, **fields):
    return await seed.subscription(db, channel_id=CHAT, url=URL, translate=False, **fields)


def _tg_update(text: str, chat_type: str):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = int(CHAT)
    update.effective_chat.type = chat_type
    update.effective_user.id = 42
    return update


async def _run_tg(text: str, *, chat_type: str = "private", member_status: str | None = None):
    update = _tg_update(text, chat_type)
    context = MagicMock()
    context.args = text.split()[1:]
    if member_status is None:
        context.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("no lookup expected"))
    else:
        context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    await template_command(update, context)
    return update, context


def _reply_texts(update) -> list[str]:
    return [call.args[0] for call in update.message.reply_text.await_args_list]


async def test_tg_show_without_template_lists_placeholders(db):
    await _tg_sub(db)
    update, _ = await _run_tg(f"/template {URL}")

    texts = _reply_texts(update)
    assert any("No template set" in t and "{title}" in t for t in texts)


async def test_tg_show_with_template_uses_pre_block(db):
    await _tg_sub(db, message_template="A\nB")
    update, _ = await _run_tg(f"/template {URL}")

    texts = _reply_texts(update)
    assert any("<pre>A\nB</pre>" in t for t in texts)


async def test_tg_set_multiline_keeps_newlines_and_previews(db):
    sub = await _tg_sub(db)
    await seed.entry(db, sub.feed_id, guid="e1", title="Hello", summary="World")
    update, _ = await _run_tg(f"/template {URL} 📌 {{title}}\n{{summary}}")

    assert await _template_of(db, sub.id) == "📌 {title}\n{summary}"
    texts = _reply_texts(update)
    assert any("latest entry" in t and "📌 Hello" in t for t in texts)


async def test_tg_set_backslash_n_is_normalized(db):
    sub = await _tg_sub(db)
    await _run_tg(f"/template {URL} {{title}}" + r"\n" + "{url}")

    assert await _template_of(db, sub.id) == "{title}\n{url}"


async def test_tg_unknown_placeholder_rejected_before_storing(db):
    sub = await _tg_sub(db)
    update, _ = await _run_tg(f"/template {URL} {{tittle}}")

    assert await _template_of(db, sub.id) is None
    texts = _reply_texts(update)
    assert any("unknown placeholder" in t and "{tittle}" in t for t in texts)


async def test_tg_reset_clears_template(db):
    sub = await _tg_sub(db, message_template="{title}")
    update, _ = await _run_tg(f"/template {URL} reset")

    assert await _template_of(db, sub.id) is None
    assert any("cleared" in t for t in _reply_texts(update))


async def test_tg_all_applies_to_channel(db):
    subs = [
        await seed.subscription(db, channel_id=CHAT, url=f"https://ex.com/{i}", translate=False)
        for i in range(3)
    ]
    update, _ = await _run_tg("/template all {title}")

    for sub in subs:
        assert await _template_of(db, sub.id) == "{title}"
    texts = _reply_texts(update)
    assert any("3 subscription(s)" in t for t in texts)
    assert any("sample data" in t for t in texts)


async def test_tg_mutations_gated_but_show_open(db):
    sub = await _tg_sub(db)

    # Bare show: the gate is never consulted, the reply still goes out.
    update, context = await _run_tg(
        f"/template {URL}", chat_type="supergroup", member_status="member"
    )
    context.bot.get_chat_member.assert_not_awaited()
    assert _reply_texts(update)

    # A mutation by a plain member: the real gate denies it and nothing is stored.
    update, context = await _run_tg(
        f"/template {URL} {{title}}", chat_type="supergroup", member_status="member"
    )
    context.bot.get_chat_member.assert_awaited_once()
    assert await _template_of(db, sub.id) is None
    assert any("group admins" in t for t in _reply_texts(update))


# ----------------------------------------------------------------- discord

CHANNEL = "555"


async def _discord_sub(db, **fields):
    return await seed.subscription(
        db, platform="discord", channel_id=CHANNEL, url=URL, translate=False, **fields
    )


def _interaction():
    interaction = MagicMock()
    interaction.channel_id = int(CHANNEL)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _run_discord(*, url: str, template=None, reset=False):
    cog = FeedCommands(MagicMock())
    interaction = _interaction()
    await FeedCommands.feed_template.callback(
        cog, interaction, url=url, template=template, reset=reset
    )
    return interaction


def _followup_texts(interaction) -> list[str]:
    return [call.args[0] for call in interaction.followup.send.await_args_list]


async def test_discord_set_and_preview(db):
    sub = await _discord_sub(db)
    await seed.entry(db, sub.feed_id, guid="e1", title="Hello", link="https://ex.com/e1")
    interaction = await _run_discord(url=URL, template=r"📌 **{title}**\n{url}")

    assert await _template_of(db, sub.id) == "📌 **{title}**\n{url}"
    texts = _followup_texts(interaction)
    assert any("latest entry" in t and "📌 **Hello**" in t for t in texts)
    assert interaction.followup.send.await_args.kwargs.get("ephemeral") is True


async def test_discord_unknown_placeholder_rejected(db):
    sub = await _discord_sub(db)
    interaction = await _run_discord(url=URL, template="{tittle}")

    assert await _template_of(db, sub.id) is None
    assert any("unknown placeholder" in t for t in _followup_texts(interaction))


async def test_discord_show_escapes_newlines_for_copy_paste(db):
    await _discord_sub(db, message_template="A\nB")
    interaction = await _run_discord(url=URL)

    assert any("A\\nB" in t for t in _followup_texts(interaction))


async def test_discord_show_without_subscription(db):
    interaction = await _run_discord(url=URL)

    assert any("No subscription" in t for t in _followup_texts(interaction))


async def test_discord_reset_all_clears_channel(db):
    subs = [
        await seed.subscription(
            db,
            platform="discord",
            channel_id=CHANNEL,
            url=f"https://ex.com/{i}",
            message_template="X",
        )
        for i in range(2)
    ]
    interaction = await _run_discord(url="all", reset=True)

    for sub in subs:
        assert await _template_of(db, sub.id) is None
    assert any("2 subscription(s)" in t for t in _followup_texts(interaction))
