"""TelegramAdapter digest delivery: HTML rendering, preview kill, fallback.

Drives send_digest_text / send_digest_text_pinned against a mocked PTB
bot — asserts the digest goes out as rendered HTML with link previews
disabled, degrades to plain text when Telegram rejects the entities, and
keeps the ChannelGone contract of the plain send path.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from newsflow.adapters.base import ChannelGoneError
from newsflow.adapters.telegram.bot import TelegramAdapter
from telegram.error import BadRequest


def _adapter():
    adapter = TelegramAdapter(token="test-token")
    adapter.app = MagicMock()
    adapter.app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter.app.bot.pin_chat_message = AsyncMock()
    return adapter


async def test_digest_sends_rendered_html_with_preview_off():
    adapter = _adapter()

    ok = await adapter.send_digest_text("123", "**Digest** & more")

    assert ok is True
    kwargs = adapter.app.bot.send_message.await_args.kwargs
    assert kwargs["text"] == "<b>Digest</b> &amp; more"
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["disable_web_page_preview"] is True


async def test_digest_falls_back_to_plain_text_on_entity_error():
    adapter = _adapter()
    adapter.app.bot.send_message = AsyncMock(
        side_effect=[BadRequest("Can't parse entities: whatever"), MagicMock(message_id=7)]
    )

    ok = await adapter.send_digest_text("123", "body **x**")

    assert ok is True
    assert adapter.app.bot.send_message.await_count == 2
    second = adapter.app.bot.send_message.await_args_list[1].kwargs
    assert second["text"] == "body **x**"  # raw text, not HTML
    assert "parse_mode" not in second  # plain send
    assert second["disable_web_page_preview"] is True


async def test_digest_gone_chat_still_raises_channel_gone():
    adapter = _adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))

    with pytest.raises(ChannelGoneError):
        await adapter.send_digest_text("123", "body")


async def test_digest_pinned_renders_html_and_pins():
    adapter = _adapter()

    with patch("newsflow.adapters.telegram.bot.get_settings") as gs:
        gs.return_value.digest_auto_pin = True
        sent, pin_id = await adapter.send_digest_text_pinned("123", "**D**")

    assert (sent, pin_id) == (True, "42")
    kwargs = adapter.app.bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["disable_web_page_preview"] is True
    adapter.app.bot.pin_chat_message.assert_awaited_once()


async def test_digest_pinned_respects_auto_pin_off():
    adapter = _adapter()

    with patch("newsflow.adapters.telegram.bot.get_settings") as gs:
        gs.return_value.digest_auto_pin = False
        sent, pin_id = await adapter.send_digest_text_pinned("123", "**D**")

    assert (sent, pin_id) == (True, None)
    adapter.app.bot.pin_chat_message.assert_not_awaited()
    # Still digest-rendered even without the pin.
    assert adapter.app.bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
