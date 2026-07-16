"""Telegram inline-keyboard UX: command menu, /list pagination, /start menu.

Covers the pure keyboard/command builders, the shared list renderer's
pagination math, and the callback router — driven with mocked I/O so no
real bot or network is involved.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.base import Message
from newsflow.adapters.telegram.bot import (
    _MENU_COMMANDS,
    WELCOME_TEXT,
    TelegramAdapter,
    _list_keyboard,
    _render_list,
    _start_menu_keyboard,
    on_callback,
)
from telegram.error import BadRequest


class _SessionCtx:
    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def _mock_sub(i: int):
    """A subscription that renders with no status chip (active, no errors)."""
    feed = MagicMock()
    feed.title = f"Feed {i}"
    feed.url = f"https://ex.com/{i}"
    feed.is_active = True
    feed.error_count = 0
    sub = MagicMock()
    sub.feed = feed
    sub.is_active = True
    sub.silent = False
    sub.translate = True
    sub.target_language = "en"
    return sub


# --- pure builders ---------------------------------------------------------


def test_menu_commands_are_telegram_valid():
    assert _MENU_COMMANDS  # non-empty
    for cmd, desc in _MENU_COMMANDS:
        assert 1 <= len(cmd) <= 32 and cmd.islower() and cmd.isascii()
        assert 1 <= len(desc) <= 256


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
    assert datas == ["menu:list", "menu:status", "menu:help"]


# --- shared list renderer --------------------------------------------------


def _patch_subs(subs):
    service = MagicMock()
    service.get_channel_subscriptions = AsyncMock(return_value=subs)
    return (
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
    )


async def test_render_list_empty_has_no_keyboard():
    p1, p2 = _patch_subs([])
    with p1, p2:
        text, keyboard = await _render_list("123", 1)
    assert "No feeds subscribed" in text
    assert keyboard is None


async def test_render_list_paginates_with_next_button():
    p1, p2 = _patch_subs([_mock_sub(i) for i in range(25)])  # 2 pages @ 20/page
    with p1, p2:
        text1, kb1 = await _render_list("123", 1)
        text2, kb2 = await _render_list("123", 2)
    assert "page 1/2" in text1 and "Feed 0" in text1 and "Feed 19" in text1
    assert kb1.inline_keyboard[0][0].callback_data == "list:2"
    assert "page 2/2" in text2 and "Feed 24" in text2
    assert kb2.inline_keyboard[0][0].callback_data == "list:1"


async def test_render_list_clamps_out_of_range_page():
    p1, p2 = _patch_subs([_mock_sub(i) for i in range(5)])  # 1 page
    with p1, p2:
        text, keyboard = await _render_list("123", 99)
    assert "Feed 0" in text
    assert keyboard is None  # single page → no buttons


# --- rendering hardening ---------------------------------------------------


async def test_render_list_escapes_language_code():
    """target_language is stored verbatim from user input; unescaped, a value
    like `<b` breaks the HTML parse for every /list in the chat."""
    sub = _mock_sub(0)
    sub.target_language = "<b"
    p1, p2 = _patch_subs([sub])
    with p1, p2:
        text, _ = await _render_list("123", 1)
    assert "🌐 &lt;b" in text


async def test_render_list_includes_paused_with_chip():
    """Paused subscriptions must stay listed (F10) — pinned at the adapter
    layer: the service must be asked for inactive rows and the ⏸ chip must
    actually render."""
    paused = _mock_sub(0)
    paused.is_active = False
    service = MagicMock()
    service.get_channel_subscriptions = AsyncMock(return_value=[paused])
    with (
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.telegram.bot.SubscriptionService", return_value=service),
    ):
        text, _ = await _render_list("123", 1)
    assert service.get_channel_subscriptions.call_args.kwargs["include_inactive"] is True
    assert "⏸" in text


async def test_render_list_pages_stay_under_telegram_limit():
    """Worst-case column-length titles/URLs (&-heavy, so HTML escaping
    expands them) must never produce a page over Telegram's 4096-char cap,
    and the budget packer must not drop any subscription."""
    subs = []
    for i in range(30):
        s = _mock_sub(i)
        s.feed.title = "T&" * 256  # 512 chars, the column max
        s.feed.url = f"https://ex.com/{i}?" + "&a=1" * 500  # ~2000 chars
        subs.append(s)
    p1, p2 = _patch_subs(subs)
    with p1, p2:
        text1, _ = await _render_list("123", 1)
        m = re.search(r"page 1/(\d+)", text1)
        total_pages = int(m.group(1)) if m else 1
        seen_titles = 0
        for p in range(1, total_pages + 1):
            text, _ = await _render_list("123", p)
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


async def test_on_callback_list_edits_in_place():
    update, query = _callback_update("list:2")
    with patch(
        "newsflow.adapters.telegram.bot._render_list",
        AsyncMock(return_value=("PAGE 2 TEXT", None)),
    ):
        await on_callback(update, MagicMock())
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.call_args.args[0] == "PAGE 2 TEXT"


async def test_on_callback_list_swallows_not_modified():
    update, query = _callback_update("list:1")
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    with patch(
        "newsflow.adapters.telegram.bot._render_list",
        AsyncMock(return_value=("X", None)),
    ):
        await on_callback(update, MagicMock())  # must not raise
    query.answer.assert_awaited_once()


async def test_on_callback_list_falls_back_when_message_inaccessible():
    """PTB 20.8 raises TypeError when the callback's message is >48h old
    (InaccessibleMessage); pagination must degrade to a fresh message
    instead of dying into the error handler with no visible effect."""
    update, query = _callback_update("list:2", chat_id=888)
    query.edit_message_text = AsyncMock(
        side_effect=TypeError("Cannot edit an inaccessible message")
    )
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    with patch(
        "newsflow.adapters.telegram.bot._render_list",
        AsyncMock(return_value=("PAGE", None)),
    ):
        await on_callback(update, context)
    query.answer.assert_awaited_once()
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 888
    assert context.bot.send_message.call_args.kwargs["text"] == "PAGE"


async def test_on_callback_menu_status_sends_new_message():
    update, query = _callback_update("menu:status", chat_id=777)
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    with patch(
        "newsflow.adapters.telegram.bot._render_status",
        AsyncMock(return_value="STATUS TEXT"),
    ):
        await on_callback(update, context)
    query.answer.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.call_args.kwargs["text"] == "STATUS TEXT"
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 777


async def test_on_callback_menu_help_sends_welcome():
    update, query = _callback_update("menu:help")
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    await on_callback(update, context)
    assert context.bot.send_message.call_args.kwargs["text"] == WELCOME_TEXT
