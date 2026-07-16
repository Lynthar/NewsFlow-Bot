"""Permission gating: Telegram group-admin checks + Discord native locks.

Telegram: state-changing commands in group chats require the group owner /
an administrator (looked up via get_chat_member with a short TTL cache),
with ADMIN_USER_IDS as a global bypass; private chats are never restricted
and read-only commands stay open to every member. Discord: the
feed/settings/digest command groups carry native default_permissions
(Manage Server) — pinned by introspection, since Discord enforces them
server-side.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import newsflow.adapters.telegram.bot as tg_bot
import pytest
from newsflow.adapters.telegram.bot import (
    _require_group_admin,
    digest_command,
    list_command,
    remove_command,
)
from newsflow.config import Settings


def _settings(admin_only: bool = True, admin_ids: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(telegram_admin_only=admin_only, admin_user_ids=list(admin_ids))


def _group_update(user_id: int = 42, chat_id: int = -100123, sender_chat=None):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.sender_chat = sender_chat
    update.effective_chat.id = chat_id
    update.effective_chat.type = "supergroup"
    update.effective_user.id = user_id
    return update


def _context(status: str = "member"):
    context = MagicMock()
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=status))
    return context


@pytest.fixture(autouse=True)
def _fresh_admin_cache():
    tg_bot._admin_cache.clear()
    yield
    tg_bot._admin_cache.clear()


# ── the gate itself ──────────────────────────────────────────────────────────


async def test_private_chat_always_allowed(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update = _group_update()
    update.effective_chat.type = "private"
    context = _context()

    assert await _require_group_admin(update, context) is True
    context.bot.get_chat_member.assert_not_awaited()


async def test_group_member_denied_with_notice(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update, context = _group_update(), _context(status="member")

    assert await _require_group_admin(update, context) is False
    notice = update.message.reply_text.call_args.args[0]
    assert "group admins" in notice


@pytest.mark.parametrize("status", ["administrator", "creator"])
async def test_group_admin_and_owner_allowed(monkeypatch, status):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update, context = _group_update(), _context(status=status)

    assert await _require_group_admin(update, context) is True
    update.message.reply_text.assert_not_awaited()


async def test_flag_off_allows_everyone(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings(admin_only=False))
    update, context = _group_update(), _context(status="member")

    assert await _require_group_admin(update, context) is True
    context.bot.get_chat_member.assert_not_awaited()


async def test_admin_user_ids_bypass(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings(admin_ids=("42",)))
    update, context = _group_update(user_id=42), _context(status="member")

    assert await _require_group_admin(update, context) is True
    context.bot.get_chat_member.assert_not_awaited()


async def test_anonymous_group_admin_allowed(monkeypatch):
    """Messages sent 'as the group' (sender_chat == the chat) come from
    anonymous admins; get_chat_member can't resolve them, but only admins
    can post that way."""
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update = _group_update(sender_chat=SimpleNamespace(id=-100123))
    context = _context(status="member")

    assert await _require_group_admin(update, context) is True
    context.bot.get_chat_member.assert_not_awaited()


async def test_verdict_is_cached(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update, context = _group_update(), _context(status="administrator")

    assert await _require_group_admin(update, context) is True
    assert await _require_group_admin(update, context) is True
    context.bot.get_chat_member.assert_awaited_once()


async def test_lookup_failure_fails_closed_and_is_not_cached(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update = _group_update()
    context = MagicMock()
    context.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("api down"))

    assert await _require_group_admin(update, context) is False
    assert "verify" in update.message.reply_text.call_args.args[0]
    # A failure verdict must not stick: the next attempt retries the lookup.
    assert await _require_group_admin(update, context) is False
    assert context.bot.get_chat_member.await_count == 2


# ── handler wiring ───────────────────────────────────────────────────────────


async def test_remove_denied_for_group_member_before_any_db_work(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update, context = _group_update(), _context(status="member")
    context.args = ["https://example.com/feed"]

    factory = MagicMock()
    with patch.object(tg_bot, "get_session_factory", factory):
        await remove_command(update, context)

    factory.assert_not_called()
    assert "group admins" in update.message.reply_text.call_args.args[0]


async def test_list_is_not_gated_in_groups(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update, context = _group_update(), _context(status="member")
    context.args = []

    with patch.object(tg_bot, "_render_list", AsyncMock(return_value=("LIST", None))):
        await list_command(update, context)

    context.bot.get_chat_member.assert_not_awaited()
    assert update.message.reply_text.call_args.args[0] == "LIST"


async def test_digest_show_open_but_enable_gated(monkeypatch):
    monkeypatch.setattr(tg_bot, "get_settings", lambda: _settings())
    update, context = _group_update(), _context(status="member")

    # enable: denied before any repository work.
    context.args = ["enable", "daily", "9"]
    await digest_command(update, context)
    assert "group admins" in update.message.reply_text.call_args.args[0]

    # show: passes the gate (no membership lookup), reaches the repo layer.
    update2, _ = _group_update(), None
    context.args = ["show"]
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)

    class _Ctx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    with (
        patch.object(tg_bot, "get_session_factory", return_value=lambda: _Ctx()),
        patch(
            "newsflow.repositories.digest_repository.ChannelDigestRepository",
            return_value=repo,
        ),
    ):
        await digest_command(update2, context)

    context.bot.get_chat_member.assert_awaited_once()  # only the enable call
    assert "No digest configured" in update2.message.reply_text.call_args.args[0]


# ── config parsing ───────────────────────────────────────────────────────────


def test_admin_user_ids_accepts_comma_form(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "123, 456")
    assert Settings(_env_file=None).admin_user_ids == ["123", "456"]


def test_admin_user_ids_accepts_json_form(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", '["123", "456"]')
    assert Settings(_env_file=None).admin_user_ids == ["123", "456"]


def test_telegram_admin_only_defaults_on():
    assert Settings(_env_file=None).telegram_admin_only is True


# ── discord: native group locks ──────────────────────────────────────────────


def test_discord_groups_locked_to_manage_guild():
    pytest.importorskip("discord")
    from discord import Permissions
    from newsflow.adapters.discord.bot import (
        DigestCommands,
        FeedCommands,
        SettingsCommands,
    )

    assert FeedCommands.feed_group.default_permissions == Permissions(manage_guild=True)
    assert SettingsCommands.settings_group.default_permissions == Permissions(manage_guild=True)
    assert DigestCommands.digest_group.default_permissions == Permissions(manage_guild=True)
    # The top-level /status stays open to everyone.
    assert SettingsCommands.status.default_permissions is None
