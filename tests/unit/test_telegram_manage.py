"""Telegram /manage: per-feed inline action buttons.

Covers the pure view builders (button payloads must round-trip through the
64-byte callback_data budget), the mg:* callback router with mocked service
I/O, and the mutating-press permission gate."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType

from newsflow.adapters.telegram.bot import (
    _admin_cache,
    _callback_user_may_manage,
    _manage_confirm_view,
    _manage_detail_view,
    _manage_list_view,
    _on_manage_callback,
)


def _mock_sub(i: int = 1, *, active=True, silent=False, chat_id="555"):
    feed = MagicMock()
    feed.title = f"Feed {i}"
    feed.url = f"https://ex.com/{i}"
    feed.is_active = True
    feed.error_count = 0
    sub = MagicMock()
    sub.id = i
    sub.feed = feed
    sub.platform = "telegram"
    sub.platform_channel_id = chat_id
    sub.is_active = active
    sub.silent = silent
    sub.translate = True
    sub.target_language = "en"
    return sub


# --- view builders -----------------------------------------------------------


def test_manage_list_one_button_per_sub_with_view_payload():
    subs = [_mock_sub(i) for i in range(1, 4)]
    text, kb = _manage_list_view(subs, 1, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["mg:v:1:1", "mg:v:2:1", "mg:v:3:1"]
    assert "3 total" in text


def test_manage_list_paginates_and_carries_target():
    subs = [_mock_sub(i) for i in range(1, 12)]  # 2 pages @ 8
    _, kb1 = _manage_list_view(subs, 1, "-1009")
    datas = [btn.callback_data for row in kb1.inline_keyboard for btn in row]
    assert datas[0] == "mg:v:1:1:-1009"
    assert datas[-1] == "mg:p:2:-1009"  # nav row


def test_manage_detail_buttons_reflect_state():
    _, kb = _manage_detail_view(_mock_sub(7, active=True, silent=False), 2, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["mg:a:pause:7:2", "mg:a:sil1:7:2", "mg:a:rm:7:2", "mg:p:2"]

    _, kb = _manage_detail_view(_mock_sub(7, active=False, silent=True), 2, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas[0] == "mg:a:resume:7:2"
    assert datas[1] == "mg:a:sil0:7:2"


def test_manage_confirm_view_requires_second_tap():
    text, kb = _manage_confirm_view(_mock_sub(7), 1, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["mg:a:rmc:7:1", "mg:v:7:1"]
    assert "Remove" in text


def test_callback_data_fits_telegram_budget():
    sub = _mock_sub(2**31, chat_id="-1001234567890123")
    for _, kb in (
        _manage_detail_view(sub, 99, "-1001234567890123"),
        _manage_confirm_view(sub, 99, "-1001234567890123"),
    ):
        for row in kb.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64, btn.callback_data


# --- permission gate ---------------------------------------------------------


def _chat(chat_type=ChatType.PRIVATE, chat_id=555):
    return SimpleNamespace(id=chat_id, type=chat_type)


def _ctx(member_status=None):
    context = MagicMock()
    if member_status is None:
        context.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("no lookup expected"))
    else:
        context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    return context


def _settings(admin_ids=(), admin_only=True):
    fake = MagicMock()
    fake.admin_user_ids = list(admin_ids)
    fake.telegram_admin_only = admin_only
    return fake


async def test_private_chat_manages_own_subs_freely():
    _admin_cache.clear()
    sub = _mock_sub(chat_id="555")
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()):
        ok = await _callback_user_may_manage(
            sub, _chat(ChatType.PRIVATE, 555), SimpleNamespace(id=1), _ctx()
        )
    assert ok is True


async def test_group_sub_requires_group_admin():
    _admin_cache.clear()
    sub = _mock_sub(chat_id="-42")
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()):
        ok = await _callback_user_may_manage(
            sub, _chat(ChatType.SUPERGROUP, -42), SimpleNamespace(id=1), _ctx("member")
        )
    assert ok is False


async def test_foreign_sub_in_group_is_always_denied():
    _admin_cache.clear()
    sub = _mock_sub(chat_id="-999")  # belongs elsewhere
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()):
        ok = await _callback_user_may_manage(
            sub, _chat(ChatType.SUPERGROUP, -42), SimpleNamespace(id=1), _ctx("administrator")
        )
    assert ok is False


async def test_channel_sub_from_dm_requires_channel_admin():
    _admin_cache.clear()
    sub = _mock_sub(chat_id="-1009")
    with patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()):
        ctx = _ctx("administrator")
        ok = await _callback_user_may_manage(
            sub, _chat(ChatType.PRIVATE, 555), SimpleNamespace(id=1), ctx
        )
    assert ok is True
    ctx.bot.get_chat_member.assert_awaited_once_with(-1009, 1)


# --- callback routing --------------------------------------------------------


def _query(data, user_id=1):
    query = MagicMock()
    query.data = data
    query.from_user = SimpleNamespace(id=user_id)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


async def test_action_press_calls_service_and_rerenders():
    _admin_cache.clear()
    sub = _mock_sub(7, chat_id="555")
    service = MagicMock()
    service.pause_subscription = AsyncMock(
        return_value=SimpleNamespace(success=True, message="Paused X")
    )

    class _SessionCtx:
        def __init__(self):
            self.session = MagicMock()
            self.session.commit = AsyncMock()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *a):
            return False

    query = _query("mg:a:pause:7:1")
    with (
        patch("newsflow.adapters.telegram.bot._load_sub", AsyncMock(return_value=sub)),
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
        patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()),
    ):
        await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    service.pause_subscription.assert_awaited_once_with("telegram", "555", "https://ex.com/7")
    toast = query.answer.await_args.args[0]
    assert toast.startswith("✅")
    query.edit_message_text.assert_awaited_once()  # detail re-rendered


async def test_action_press_denied_for_non_admin_in_group():
    _admin_cache.clear()
    sub = _mock_sub(7, chat_id="-42")
    service = MagicMock()
    service.pause_subscription = AsyncMock()

    query = _query("mg:a:pause:7:1")
    context = MagicMock()
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    with (
        patch("newsflow.adapters.telegram.bot._load_sub", AsyncMock(return_value=sub)),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
        patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()),
    ):
        await _on_manage_callback(query, _chat(ChatType.SUPERGROUP, -42), context, query.data)

    service.pause_subscription.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


async def test_remove_needs_confirmation_before_mutating():
    _admin_cache.clear()
    sub = _mock_sub(7, chat_id="555")
    service = MagicMock()
    service.unsubscribe = AsyncMock()

    query = _query("mg:a:rm:7:1")
    with (
        patch("newsflow.adapters.telegram.bot._load_sub", AsyncMock(return_value=sub)),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
        patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()),
    ):
        await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    service.unsubscribe.assert_not_awaited()
    rendered = query.edit_message_text.await_args.args[0]
    assert "Remove" in rendered


async def test_stale_sub_id_falls_back_to_list():
    _admin_cache.clear()
    query = _query("mg:a:pause:404:1")
    with (
        patch("newsflow.adapters.telegram.bot._load_sub", AsyncMock(return_value=None)),
        patch(
            "newsflow.adapters.telegram.bot._load_channel_subs",
            AsyncMock(return_value=[]),
        ),
        patch("newsflow.adapters.telegram.bot.get_settings", return_value=_settings()),
    ):
        await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    assert query.answer.await_args_list[0].kwargs.get("show_alert") is True
    query.edit_message_text.assert_awaited_once()  # list re-rendered
