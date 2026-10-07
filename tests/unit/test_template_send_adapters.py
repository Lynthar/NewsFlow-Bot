"""Adapter send paths for template-rendered messages.

Telegram: Markdown → HTML conversion, entity-rejection fallback to plain
text, and the oversized-HTML guard (a template send must never enter a
permanent BadRequest retry loop). Discord: plain-content send, the
image-only side embed, the 2000-char cap, and the default embed path
staying untouched when no template is set.
"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from newsflow.adapters.base import UndeliverableError
from tests import seed

# ---------------------------------------------------------------- telegram


async def test_telegram_template_sends_converted_html():
    adapter = seed.tg_adapter()
    ok = await adapter.send_message(
        "123", seed.message(template_text="**Hi** [x](https://e.io/?a=1&b=2)")
    )

    assert ok is True
    adapter.app.bot.send_message.assert_awaited_once()
    kwargs = adapter.app.bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>Hi</b>" in kwargs["text"]
    assert '<a href="https://e.io/?a=1&amp;b=2">x</a>' in kwargs["text"]
    # Entry messages keep link previews on, matching the default layout.
    assert kwargs["disable_web_page_preview"] is False


async def test_telegram_no_template_uses_default_layout():
    adapter = seed.tg_adapter()
    ok = await adapter.send_message("123", seed.message())

    assert ok is True
    text = adapter.app.bot.send_message.await_args.kwargs["text"]
    assert text.startswith("<b>T</b>")  # _format_message layout


async def test_telegram_entity_rejection_falls_back_to_plain():
    from telegram.error import BadRequest

    adapter = seed.tg_adapter()
    adapter.app.bot.send_message = AsyncMock(
        side_effect=[BadRequest("Can't parse entities: unsupported start tag"), MagicMock()]
    )

    ok = await adapter.send_message("123", seed.message(template_text="**broken"))

    assert ok is True
    assert adapter.app.bot.send_message.await_count == 2
    retry_kwargs = adapter.app.bot.send_message.await_args_list[1].kwargs
    assert "parse_mode" not in retry_kwargs
    assert retry_kwargs["text"] == "**broken"


async def test_telegram_non_entity_bad_request_on_template_sends_title_and_link():
    from telegram.error import BadRequest

    adapter = seed.tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=[BadRequest("Message is too long"), None])

    ok = await adapter.send_message("123", seed.message(template_text="{x}"))

    # Not the plain rendering of the template: a non-entity refusal skips to the bare form.
    assert ok is True
    bare = adapter.app.bot.send_message.await_args.kwargs
    assert "{x}" not in bare["text"]
    assert bare["disable_web_page_preview"] is True


async def test_telegram_oversized_html_sends_plain_text():
    adapter = seed.tg_adapter()
    # 3400 ampersands: fits as Markdown, but entity-escapes to 17k chars.
    template = "&" * 3400
    ok = await adapter.send_message("123", seed.message(template_text=template))

    assert ok is True
    kwargs = adapter.app.bot.send_message.await_args.kwargs
    assert "parse_mode" not in kwargs
    assert kwargs["text"] == template


async def test_telegram_overlong_markdown_is_truncated():
    adapter = seed.tg_adapter()
    ok = await adapter.send_message("123", seed.message(template_text="a" * 4000))

    assert ok is True
    text = adapter.app.bot.send_message.await_args.kwargs["text"]
    assert len(text) == 3500
    assert text.endswith("…")


# ----------------------------------------------------------------- discord


async def test_discord_template_sends_plain_content():
    adapter, channel = seed.discord_adapter()
    ok = await adapter.send_message("42", seed.message(template_text="📌 **Big** news"))

    assert ok is True
    call = channel.send.await_args
    assert call.args[0] == "📌 **Big** news"
    # Template content is feed-controlled text — the send must carry the
    # ping-safe allowance (nothing enabled) alongside it.
    allowed = call.kwargs["allowed_mentions"]
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False


async def test_discord_template_with_image_attaches_image_only_embed():
    adapter, channel = seed.discord_adapter()
    ok = await adapter.send_message(
        "42", seed.message(template_text="text", image_url="https://x.test/i.png")
    )

    assert ok is True
    call = channel.send.await_args
    assert call.kwargs["content"] == "text"
    embed = call.kwargs["embed"]
    assert embed.image.url == "https://x.test/i.png"
    assert embed.description is None  # image-only: text authority stays with the template


async def test_discord_template_content_is_capped_at_2000():
    adapter, channel = seed.discord_adapter()
    ok = await adapter.send_message("42", seed.message(template_text="a" * 2500))

    assert ok is True
    content = channel.send.await_args.args[0]
    assert len(content) == 2000
    assert content.endswith("…")


async def test_discord_no_template_keeps_embed_layout():
    adapter, channel = seed.discord_adapter()
    ok = await adapter.send_message("42", seed.message())

    assert ok is True
    call = channel.send.await_args
    assert not call.args
    assert "content" not in call.kwargs
    assert call.kwargs["embed"].title == "T"
    assert call.kwargs["embed"].url == "https://x.test/a"


# Discord refuses the whole message over an image that isn't an absolute http(s)
# URL, so such an image would fail the entry on every retry.
_UNUSABLE_IMAGES = ["/img/cover.jpg", "//cdn.x.test/c.jpg", "data:image/png;base64,AAAA"]


@pytest.mark.parametrize("image_url", _UNUSABLE_IMAGES)
async def test_discord_embed_leaves_out_an_image_discord_refuses(image_url):
    adapter, channel = seed.discord_adapter()

    assert await adapter.send_message("42", seed.message(image_url=image_url)) is True
    assert channel.send.await_args.kwargs["embed"].image.url is None


@pytest.mark.parametrize("image_url", _UNUSABLE_IMAGES)
async def test_discord_template_leaves_out_an_image_discord_refuses(image_url):
    adapter, channel = seed.discord_adapter()
    message = seed.message(template_text="text", image_url=image_url)

    assert await adapter.send_message("42", message) is True
    assert "embed" not in channel.send.await_args.kwargs


async def test_discord_embed_drops_a_link_discord_would_reject():
    adapter, channel = seed.discord_adapter()

    assert await adapter.send_message("42", seed.message(link="https://x.test/a b")) is True
    assert channel.send.await_args.kwargs["embed"].url is None


def _discord_http_error(status: int, code: int) -> discord.HTTPException:
    response = MagicMock(status=status, reason="refused")
    return discord.HTTPException(response, {"code": code, "message": "refused"})


async def test_discord_400_resends_the_entry_as_escaped_title_and_link():
    adapter, channel = seed.discord_adapter()
    channel.send.side_effect = [_discord_http_error(400, 50035), None]

    ok = await adapter.send_message("42", seed.message(title="**T**", link="https://x.test/a"))

    assert ok is True
    bare = channel.send.await_args
    assert bare.args[0] == "\\*\\*T**\nhttps://x.test/a"
    assert "embed" not in bare.kwargs


async def test_discord_400_on_title_and_link_too_is_undeliverable():
    adapter, channel = seed.discord_adapter()
    channel.send.side_effect = _discord_http_error(400, 50035)

    with pytest.raises(UndeliverableError):
        await adapter.send_message("42", seed.message())
    assert channel.send.await_count == 2


async def test_discord_5xx_is_not_retried_as_title_and_link():
    adapter, channel = seed.discord_adapter()
    channel.send.side_effect = _discord_http_error(503, 0)

    assert await adapter.send_message("42", seed.message()) is False
    assert channel.send.await_count == 1
