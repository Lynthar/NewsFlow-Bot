"""Telegram channel support via private-chat binding (_resolve_target).

Channels can't host commands (no sender; PTB's CommandHandler ignores
channel_post), so admins manage a channel by DM-ing the bot with a
leading channel reference: `/add @mychannel <url>`. These tests pin the
resolution rules: passthrough outside the binding form, bot-must-see-
channel, caller-must-be-channel-admin, and the arg-stripping contract.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.telegram.bot import _admin_cache, _resolve_target
from telegram.constants import ChatMemberStatus, ChatType


def _update(chat_type=ChatType.PRIVATE, chat_id=555, user_id=42):
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat = SimpleNamespace(id=chat_id, type=chat_type)
    update.effective_user = SimpleNamespace(id=user_id)
    return update


def _context(args, *, chat=None, member_status=ChatMemberStatus.ADMINISTRATOR):
    context = MagicMock()
    context.args = args
    context.bot.get_chat = AsyncMock(return_value=chat)
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    return context


def _channel(chat_id=-1001234, username="mychannel"):
    return SimpleNamespace(id=chat_id, type=ChatType.CHANNEL, username=username)


def _fresh_settings():
    fake = MagicMock()
    fake.admin_user_ids = []
    return fake


async def test_passthrough_without_channel_ref():
    _admin_cache.clear()
    update = _update()
    context = _context(["https://example.com/feed"])
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved == ("555", ["https://example.com/feed"])
    context.bot.get_chat.assert_not_awaited()


async def test_passthrough_in_group_even_with_ref_like_arg():
    _admin_cache.clear()
    update = _update(chat_type=ChatType.GROUP, chat_id=-987)
    context = _context(["@mychannel", "url"])
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved == ("-987", ["@mychannel", "url"])


async def test_channel_admin_gets_target_and_stripped_args():
    _admin_cache.clear()
    update = _update()
    context = _context(["@mychannel", "https://example.com/feed"], chat=_channel())
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved == ("-1001234", ["https://example.com/feed"])
    context.bot.get_chat.assert_awaited_once_with("@mychannel")


async def test_raw_channel_id_reference_works():
    _admin_cache.clear()
    update = _update()
    context = _context(["-1001234", "url"], chat=_channel())
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved == ("-1001234", ["url"])
    context.bot.get_chat.assert_awaited_once_with(-1001234)


async def test_non_admin_is_denied():
    _admin_cache.clear()
    update = _update()
    context = _context(
        ["@mychannel", "url"], chat=_channel(), member_status=ChatMemberStatus.MEMBER
    )
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved is None
    update.message.reply_text.assert_awaited_once()


async def test_unreachable_channel_is_reported():
    _admin_cache.clear()
    update = _update()
    context = _context(["@mychannel", "url"])
    context.bot.get_chat = AsyncMock(side_effect=RuntimeError("chat not found"))
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved is None
    text = update.message.reply_text.await_args.args[0]
    assert "administrator" in text


async def test_group_reference_is_rejected_not_a_channel():
    _admin_cache.clear()
    update = _update()
    group = SimpleNamespace(id=-333, type=ChatType.SUPERGROUP, username="somegroup")
    context = _context(["@somegroup", "url"], chat=group)
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_fresh_settings()):
        resolved = await _resolve_target(update, context)
    assert resolved is None


async def test_admin_user_ids_bypass_membership_check():
    _admin_cache.clear()
    update = _update(user_id=42)
    context = _context(["@mychannel", "url"], chat=_channel())
    context.bot.get_chat_member = AsyncMock(side_effect=AssertionError("must not be called"))
    fake = MagicMock()
    fake.admin_user_ids = ["42"]
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=fake):
        resolved = await _resolve_target(update, context)
    assert resolved == ("-1001234", ["url"])
