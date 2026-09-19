"""Telegram inline-keyboard UX: command menu, /list pagination, /start menu.

Covers the pure keyboard/command builders, the shared list renderer's
pagination math against the real database, and the callback router — the
bot side is mocked, no network is involved.
"""

import re
from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from newsflow.adapters.base import Message
from newsflow.adapters.telegram.bot import (
    _COMMANDS,
    _MENU_COMMANDS,
    WELCOME_TEXT,
    TelegramAdapter,
    _list_keyboard,
    _render_list,
    _start_menu_keyboard,
    on_callback,
)
from tests import seed

CHAT = "123"


async def _subscribed(db, count: int, chat_id: str = CHAT, **fields):
    """`count` healthy subscriptions "Feed 0".."Feed n-1" in one chat, in id order."""
    columns = {"translate": True, "target_language": "en", **fields}
    return [
        await seed.subscription(
            db, channel_id=chat_id, url=f"https://ex.com/{i}", title=f"Feed {i}", **columns
        )
        for i in range(count)
    ]


# --- pure builders ---------------------------------------------------------


def test_menu_commands_are_telegram_valid():
    assert _MENU_COMMANDS  # non-empty
    for cmd, desc in _MENU_COMMANDS:
        assert 1 <= len(cmd) <= 32 and cmd.islower() and cmd.isascii()
        assert 1 <= len(desc) <= 256


def test_help_text_and_menu_name_exactly_the_registered_commands():
    """A /command line in the help text with no handler behind it, or a handler
    the help text never mentions, both fail here. /start is the one exception:
    it is the message itself. The menu is a curated subset, never a superset."""
    registered = {name for name, _ in _COMMANDS}
    documented = set(re.findall(r"^/([a-z]+)", WELCOME_TEXT, re.M))
    assert documented == registered - {"start"}
    assert {cmd for cmd, _ in _MENU_COMMANDS} <= registered


def test_list_keyboard_single_page_is_none():
    assert _list_keyboard(1, 1) is None


def test_list_keyboard_first_page_has_next_only():
    buttons = _list_keyboard(1, 3).inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["list:2"]


def test_list_keyboard_middle_page_has_prev_and_next():
    buttons = _list_keyboard(2, 3).inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["list:1", "list:3"]


def test_list_keyboard_last_page_has_prev_only():
    buttons = _list_keyboard(3, 3).inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["list:2"]


def test_start_menu_keyboard_callback_data():
    kb = _start_menu_keyboard()
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["menu:list", "menu:manage", "menu:status", "menu:help"]


# --- shared list renderer --------------------------------------------------


async def test_render_list_empty_has_no_keyboard(db):
    text, keyboard = await _render_list(CHAT, 1)
    assert "No feeds subscribed" in text
    assert keyboard is None


async def test_render_list_paginates_with_next_button(db):
    await _subscribed(db, 25)  # 2 pages @ 20/page
    text1, kb1 = await _render_list(CHAT, 1)
    text2, kb2 = await _render_list(CHAT, 2)
    assert "page 1/2" in text1 and "Feed 0" in text1 and "Feed 19" in text1
    assert kb1.inline_keyboard[0][0].callback_data == "list:2"
    assert "page 2/2" in text2 and "Feed 24" in text2
    assert kb2.inline_keyboard[0][0].callback_data == "list:1"


async def test_render_list_clamps_out_of_range_page(db):
    await _subscribed(db, 5)  # 1 page
    text, keyboard = await _render_list(CHAT, 99)
    assert "Feed 0" in text
    assert keyboard is None  # single page → no buttons


# --- rendering hardening ---------------------------------------------------


async def test_render_list_escapes_language_code(db):
    """target_language is stored verbatim from user input; unescaped, a value
    like `<b` breaks the HTML parse for every /list in the chat."""
    await _subscribed(db, 1, target_language="<b")
    text, _ = await _render_list(CHAT, 1)
    assert "🌐 &lt;b" in text


async def test_render_list_includes_paused_with_chip(db):
    """Paused subscriptions must stay listed (F10): the renderer asks for
    inactive rows and the ⏸ chip actually renders."""
    await _subscribed(db, 1, is_active=False)
    text, _ = await _render_list(CHAT, 1)
    assert "Feed 0" in text
    assert "⏸" in text


async def test_render_list_pages_stay_under_telegram_limit(db):
    """Worst-case column-length titles/URLs (&-heavy, so HTML escaping
    expands them) must never produce a page over Telegram's 4096-char cap,
    and the budget packer must not drop any subscription."""
    for i in range(30):
        await seed.subscription(
            db,
            channel_id=CHAT,
            title="T&" * 256,  # 512 chars, the column max
            url=f"https://ex.com/{i}?" + "&a=1" * 500,  # ~2000 chars
            translate=True,
            target_language="en",
        )
    text1, _ = await _render_list(CHAT, 1)
    m = re.search(r"page 1/(\d+)", text1)
    total_pages = int(m.group(1)) if m else 1
    seen_titles = 0
    for p in range(1, total_pages + 1):
        text, _ = await _render_list(CHAT, p)
        assert len(text) <= 4096
        seen_titles += text.count("<b>") - 1  # header contributes one <b>
    assert seen_titles == 30


def test_format_message_escapes_feed_controlled_fields():
    """Delivery-path escaping (title/summary/link/source) pinned at the
    serialization level — feeds routinely carry & and angle brackets."""
    adapter = TelegramAdapter(token="t")
    m = Message(
        title="A & B <script>",
        summary="S & <i>",
        link="https://x.test/?a=1&b=2",
        source="Ex & Co",
    )
    out = adapter._format_message(m)
    assert "A &amp; B &lt;script&gt;" in out
    assert "S &amp; &lt;i&gt;" in out
    assert 'href="https://x.test/?a=1&amp;b=2"' in out
    assert "📰 Ex &amp; Co" in out


# --- callback router -------------------------------------------------------


def _callback_update(data: str, chat_id: int = 555):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = chat_id
    return update, query


async def test_on_callback_ignores_query_without_data():
    update = MagicMock()
    update.callback_query = None
    await on_callback(update, MagicMock())  # must not raise


async def test_on_callback_list_edits_in_place(db):
    await _subscribed(db, 25, chat_id="555")
    update, query = _callback_update("list:2")
    await on_callback(update, MagicMock())
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    assert "page 2/2" in query.edit_message_text.call_args.args[0]


async def test_on_callback_list_swallows_not_modified(db):
    update, query = _callback_update("list:1")
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    await on_callback(update, MagicMock())  # must not raise
    query.answer.assert_awaited_once()


async def test_on_callback_list_falls_back_when_message_inaccessible(db):
    """PTB 20.8 raises TypeError when the callback's message is >48h old
    (InaccessibleMessage); pagination must degrade to a fresh message
    instead of dying into the error handler with no visible effect."""
    update, query = _callback_update("list:2", chat_id=888)
    query.edit_message_text = AsyncMock(
        side_effect=TypeError("Cannot edit an inaccessible message")
    )
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    await on_callback(update, context)
    query.answer.assert_awaited_once()
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 888
    assert "No feeds subscribed" in context.bot.send_message.call_args.kwargs["text"]


async def test_on_callback_menu_status_sends_new_message(db):
    await _subscribed(db, 2, chat_id="777")
    update, query = _callback_update("menu:status", chat_id=777)
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    await on_callback(update, context)
    query.answer.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    assert "Chat Subscriptions: 2" in context.bot.send_message.call_args.kwargs["text"]
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 777


async def test_on_callback_menu_help_sends_welcome():
    update, query = _callback_update("menu:help")
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    await on_callback(update, context)
    assert context.bot.send_message.call_args.kwargs["text"] == WELCOME_TEXT


# ===== strict on/off argument parsing =====


def test_parse_on_off_accepts_explicit_forms_only():
    """ "onn" (a typo for on) used to parse as False and silently DISABLE
    the setting; unknown words must return None so commands show usage."""
    from newsflow.adapters.telegram.bot import _parse_on_off

    for raw in ("on", "ON", "true", "yes", "1", "enable", "enabled"):
        assert _parse_on_off(raw) is True, raw
    for raw in ("off", "OFF", "false", "no", "0", "disable", "disabled"):
        assert _parse_on_off(raw) is False, raw
    for raw in ("onn", "of", "banana", "", " ", "2"):
        assert _parse_on_off(raw) is None, raw
