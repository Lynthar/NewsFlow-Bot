"""Telegram /manage: per-feed inline action buttons.

Covers the pure view builders (button payloads must round-trip through the
64-byte callback_data budget), the mg:* callback router against the real
database, and the mutating-press permission gate."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.constants import ChatType

from newsflow.adapters.telegram.bot import (
    _admin_cache,
    _callback_user_may_manage,
    _manage_confirm_view,
    _manage_detail_view,
    _manage_list_view,
    _on_manage_callback,
)
from newsflow.models.feed import Feed
from newsflow.models.subscription import Subscription
from tests import seed


def _sub(i: int = 1, *, active=True, silent=False, chat_id="555") -> Subscription:
    """A subscription row as the views see it, never written to a database."""
    feed = Feed(url=f"https://ex.com/{i}", title=f"Feed {i}", is_active=True, error_count=0)
    return Subscription(
        id=i,
        feed=feed,
        platform="telegram",
        platform_user_id="1",
        platform_channel_id=chat_id,
        is_active=active,
        silent=silent,
        translate=True,
        target_language="en",
    )


# --- view builders -----------------------------------------------------------


def test_manage_list_one_button_per_sub_with_view_payload():
    subs = [_sub(i) for i in range(1, 4)]
    text, kb = _manage_list_view(subs, 1, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["mg:v:1:1", "mg:v:2:1", "mg:v:3:1"]
    assert "3 total" in text


def test_manage_list_paginates_and_carries_target():
    subs = [_sub(i) for i in range(1, 12)]  # 2 pages @ 8
    _, kb1 = _manage_list_view(subs, 1, "-1009")
    datas = [btn.callback_data for row in kb1.inline_keyboard for btn in row]
    assert datas[0] == "mg:v:1:1:-1009"
    assert datas[-1] == "mg:p:2:-1009"  # nav row


def test_manage_detail_buttons_reflect_state():
    _, kb = _manage_detail_view(_sub(7, active=True, silent=False), 2, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["mg:a:pause:7:2", "mg:a:sil1:7:2", "mg:a:rm:7:2", "mg:p:2"]

    _, kb = _manage_detail_view(_sub(7, active=False, silent=True), 2, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas[0] == "mg:a:resume:7:2"
    assert datas[1] == "mg:a:sil0:7:2"


def test_manage_confirm_view_requires_second_tap():
    text, kb = _manage_confirm_view(_sub(7), 1, None)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["mg:a:rmc:7:1", "mg:v:7:1"]
    assert "Remove" in text


def test_callback_data_fits_telegram_budget():
    sub = _sub(2**31, chat_id="-1001234567890123")
    for _, kb in (
        _manage_detail_view(sub, 99, "-1001234567890123"),
        _manage_confirm_view(sub, 99, "-1001234567890123"),
    ):
        for row in kb.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64, btn.callback_data


# --- permission gate ---------------------------------------------------------
# The autouse settings baseline is TELEGRAM_ADMIN_ONLY=true with no ADMIN_USER_IDS.


def _chat(chat_type=ChatType.PRIVATE, chat_id=555):
    return SimpleNamespace(id=chat_id, type=chat_type)


def _ctx(member_status=None):
    context = MagicMock()
    if member_status is None:
        context.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("no lookup expected"))
    else:
        context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    return context


async def test_private_chat_manages_own_subs_freely():
    _admin_cache.clear()
    sub = _sub(chat_id="555")
    ok = await _callback_user_may_manage(
        sub, _chat(ChatType.PRIVATE, 555), SimpleNamespace(id=1), _ctx()
    )
    assert ok is True


async def test_group_sub_requires_group_admin():
    _admin_cache.clear()
    sub = _sub(chat_id="-42")
    ok = await _callback_user_may_manage(
        sub, _chat(ChatType.SUPERGROUP, -42), SimpleNamespace(id=1), _ctx("member")
    )
    assert ok is False


async def test_foreign_sub_in_group_is_always_denied():
    _admin_cache.clear()
    sub = _sub(chat_id="-999")  # belongs elsewhere
    ok = await _callback_user_may_manage(
        sub, _chat(ChatType.SUPERGROUP, -42), SimpleNamespace(id=1), _ctx("administrator")
    )
    assert ok is False


async def test_channel_sub_from_dm_requires_channel_admin():
    _admin_cache.clear()
    sub = _sub(chat_id="-1009")
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


async def test_action_press_pauses_the_subscription_and_rerenders(db):
    sub = await seed.subscription(db, channel_id="555", url="https://ex.com/7", title="Feed 7")

    query = _query(f"mg:a:pause:{sub.id}:1")
    await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    row = await seed.subscription_row(db, sub.id)
    assert row is not None and row.is_active is False
    toast = query.answer.await_args.args[0]
    assert toast.startswith("✅")
    query.edit_message_text.assert_awaited_once()  # detail re-rendered


async def test_action_press_denied_for_non_admin_in_group(db):
    _admin_cache.clear()
    sub = await seed.subscription(db, channel_id="-42", url="https://ex.com/7")

    query = _query(f"mg:a:pause:{sub.id}:1")
    context = MagicMock()
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    await _on_manage_callback(query, _chat(ChatType.SUPERGROUP, -42), context, query.data)

    row = await seed.subscription_row(db, sub.id)
    assert row is not None and row.is_active is True  # nothing changed
    assert query.answer.await_args.kwargs.get("show_alert") is True


async def test_remove_needs_confirmation_before_mutating(db):
    sub = await seed.subscription(db, channel_id="555", url="https://ex.com/7")

    query = _query(f"mg:a:rm:{sub.id}:1")
    await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    assert await seed.subscription_row(db, sub.id) is not None  # still subscribed
    rendered = query.edit_message_text.await_args.args[0]
    assert "Remove" in rendered


async def test_confirmed_remove_deletes_the_subscription(db):
    sub = await seed.subscription(db, channel_id="555", url="https://ex.com/7")

    query = _query(f"mg:a:rmc:{sub.id}:1")
    await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    assert await seed.subscription_row(db, sub.id) is None
    assert query.answer.await_args.args[0].startswith("✅")
    query.edit_message_text.assert_awaited_once()  # list re-rendered


async def test_stale_sub_id_falls_back_to_list(db):
    query = _query("mg:a:pause:404:1")
    await _on_manage_callback(query, _chat(ChatType.PRIVATE, 555), MagicMock(), query.data)

    assert query.answer.await_args_list[0].kwargs.get("show_alert") is True
    query.edit_message_text.assert_awaited_once()  # list re-rendered
