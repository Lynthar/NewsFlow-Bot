"""Adapter send paths for template-rendered messages.

Telegram: Markdown → HTML conversion, entity-rejection fallback to plain
text, and the oversized-HTML guard (a template send must never enter a
permanent BadRequest retry loop). Discord: plain-content send, the
image-only side embed, the 2000-char cap, and the default embed path
staying untouched when no template is set.
"""

from unittest.mock import AsyncMock, MagicMock

import discord

from newsflow.adapters.base import Message
from newsflow.adapters.discord.bot import DiscordAdapter
from newsflow.adapters.telegram.bot import TelegramAdapter


def _msg(**overrides) -> Message:
    fields: dict = dict(
        title="T",
        summary="S",
        link="https://x.test/a",
        source="x.test",
        template_text=None,
    )
    fields.update(overrides)
    return Message(**fields)


# ---------------------------------------------------------------- telegram


def _tg_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(token="test-token")
    adapter.app = MagicMock()
    adapter.app.bot.send_message = AsyncMock()
    return adapter


async def test_telegram_template_sends_converted_html():
    adapter = _tg_adapter()
    ok = await adapter.send_message("123", _msg(template_text="**Hi** [x](https://e.io/?a=1&b=2)"))

    assert ok is True
    adapter.app.bot.send_message.assert_awaited_once()
    kwargs = adapter.app.bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>Hi</b>" in kwargs["text"]
    assert '<a href="https://e.io/?a=1&amp;b=2">x</a>' in kwargs["text"]
    # Entry messages keep link previews on, matching the default layout.
    assert kwargs["disable_web_page_preview"] is False


async def test_telegram_no_template_uses_default_layout():
    adapter = _tg_adapter()
    ok = await adapter.send_message("123", _msg())

    assert ok is True
    text = adapter.app.bot.send_message.await_args.kwargs["text"]
    assert text.startswith("<b>T</b>")  # _format_message layout


async def test_telegram_entity_rejection_falls_back_to_plain():
    from telegram.error import BadRequest

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(
        side_effect=[BadRequest("Can't parse entities: unsupported start tag"), MagicMock()]
    )

    ok = await adapter.send_message("123", _msg(template_text="**broken"))

    assert ok is True
    assert adapter.app.bot.send_message.await_count == 2
    retry_kwargs = adapter.app.bot.send_message.await_args_list[1].kwargs
    assert "parse_mode" not in retry_kwargs
    assert retry_kwargs["text"] == "**broken"


async def test_telegram_non_entity_bad_request_is_not_swallowed():
    from telegram.error import BadRequest

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=BadRequest("Message is too long"))

    ok = await adapter.send_message("123", _msg(template_text="{x}"))

    # Falls through to the generic failure path: one attempt, retried
    # next cycle — no bogus plain-text resend of an unrelated error.
    assert ok is False
    assert adapter.app.bot.send_message.await_count == 1


async def test_telegram_oversized_html_sends_plain_text():
    adapter = _tg_adapter()
    # 3400 ampersands: fits as Markdown, but entity-escapes to 17k chars.
    template = "&" * 3400
    ok = await adapter.send_message("123", _msg(template_text=template))

    assert ok is True
    kwargs = adapter.app.bot.send_message.await_args.kwargs
    assert "parse_mode" not in kwargs
    assert kwargs["text"] == template


async def test_telegram_overlong_markdown_is_truncated():
    adapter = _tg_adapter()
    ok = await adapter.send_message("123", _msg(template_text="a" * 4000))

    assert ok is True
    text = adapter.app.bot.send_message.await_args.kwargs["text"]
    assert len(text) == 3500
    assert text.endswith("…")


# ----------------------------------------------------------------- discord


def _discord_adapter() -> tuple[DiscordAdapter, MagicMock]:
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    adapter.bot = MagicMock()
    adapter.bot.get_channel = MagicMock(return_value=channel)
    return adapter, channel


async def test_discord_template_sends_plain_content():
    adapter, channel = _discord_adapter()
    ok = await adapter.send_message("42", _msg(template_text="📌 **Big** news"))

    assert ok is True
    call = channel.send.await_args
    assert call.args[0] == "📌 **Big** news"
    # Template content is feed-controlled text — the send must carry the
    # ping-safe allowance (nothing enabled) alongside it.
    allowed = call.kwargs["allowed_mentions"]
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False


async def test_discord_template_with_image_attaches_image_only_embed():
    adapter, channel = _discord_adapter()
    ok = await adapter.send_message(
        "42", _msg(template_text="text", image_url="https://x.test/i.png")
    )

    assert ok is True
    call = channel.send.await_args
    assert call.kwargs["content"] == "text"
    embed = call.kwargs["embed"]
    assert embed.image.url == "https://x.test/i.png"
    assert embed.description is None  # image-only: text authority stays with the template


async def test_discord_template_content_is_capped_at_2000():
    adapter, channel = _discord_adapter()
    ok = await adapter.send_message("42", _msg(template_text="a" * 2500))

    assert ok is True
    content = channel.send.await_args.args[0]
    assert len(content) == 2000
    assert content.endswith("…")


async def test_discord_no_template_keeps_embed_layout():
    adapter, channel = _discord_adapter()
    ok = await adapter.send_message("42", _msg())

    assert ok is True
    call = channel.send.await_args
    assert not call.args
    assert "content" not in call.kwargs
    assert call.kwargs["embed"].description == "[T](https://x.test/a)"
