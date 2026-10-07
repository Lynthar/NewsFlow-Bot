"""TelegramAdapter default entry layout: length budget, attribute escaping,
show_image → link-preview mapping, and the plain-text fallback.

The default path used to have none of these guards (the template path did),
so a max-field entry rendered 11k+ chars — a deterministic "message is too
long" BadRequest that the dispatcher would retry every cycle forever.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from newsflow.adapters.base import Message
from newsflow.adapters.telegram.bot import TelegramAdapter


def _adapter():
    adapter = TelegramAdapter(token="test-token")
    adapter.app = MagicMock()
    adapter.app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return adapter


def _message(**overrides):
    values = dict(
        title="Title",
        summary="Summary",
        link="https://example.com/a",
        source="Example",
        published_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    )
    values.update(overrides)
    return Message(**values)


# ===== length budget =====


def test_format_message_stays_within_telegram_cap_on_max_fields():
    """Column-cap-sized fields made of `&` (worst escaping growth: ×5)
    used to render 11k+ chars. The budget must land the text ≤ 4096."""
    adapter = _adapter()
    msg = _message(
        title="&" * 1024,  # FeedEntry.title column cap
        summary="&" * 1024,  # MAX_SUMMARY_LENGTH
        link="https://example.com/?" + "&x=1" * 500,  # near link column cap
    )
    text = adapter._format_message(msg)
    assert len(text) <= 4096
    # The link must survive the shrinking — it's the article.
    assert 'href="' in text


def test_format_message_normal_entry_unchanged_layout():
    adapter = _adapter()
    text = adapter._format_message(_message())
    assert text.startswith("<b>Title</b>")
    assert "Summary" in text
    assert '🔗 <a href="https://example.com/a">Read more</a>' in text
    assert "📰 Example" in text


def test_format_message_drops_summary_before_title():
    """First shrink step: lose the summary, keep the full title."""
    adapter = _adapter()
    msg = _message(title="T" * 100, summary="&" * 1024, link="https://e.com/?" + "&a" * 500)
    text = adapter._format_message(msg)
    assert len(text) <= 4096
    assert "<b>" + "T" * 100 + "</b>" in text  # title intact
    assert "&amp;&amp;" not in text  # consecutive-& summary was dropped


# ===== href attribute escaping =====


def test_quote_in_link_is_escaped_in_href():
    """A raw `"` in the URL would terminate the href attribute early and
    make Telegram reject the whole message's entities."""
    adapter = _adapter()
    text = adapter._format_message(_message(link='https://example.com/a"b'))
    assert 'href="https://example.com/a&quot;b"' in text


def test_link_that_is_not_http_gets_no_href():
    # Telegram renders a tg://user link as a mention of that user.
    text = _adapter()._format_message(_message(link="tg://user?id=123456"))
    assert "href" not in text
    assert "tg://" not in text


# ===== show_image → link preview =====


async def test_show_image_false_disables_link_preview():
    adapter = _adapter()
    ok = await adapter.send_message("123", _message(show_image=False))
    assert ok is True
    assert adapter.app.bot.send_message.await_args.kwargs["disable_web_page_preview"] is True


async def test_show_image_default_keeps_link_preview():
    adapter = _adapter()
    ok = await adapter.send_message("123", _message())
    assert ok is True
    assert adapter.app.bot.send_message.await_args.kwargs["disable_web_page_preview"] is False


async def test_template_path_honors_show_image():
    adapter = _adapter()
    ok = await adapter.send_message("123", _message(template_text="**T** body", show_image=False))
    assert ok is True
    assert adapter.app.bot.send_message.await_args.kwargs["disable_web_page_preview"] is True


# ===== entity-rejection fallback =====


async def test_entity_rejection_falls_back_to_plain_text():
    """A deterministic entity BadRequest must not become an infinite
    retry — the adapter degrades to an unformatted send and reports
    success so the entry marks sent."""
    adapter = _adapter()
    adapter.app.bot.send_message = AsyncMock(
        side_effect=[BadRequest("Can't parse entities: whatever"), MagicMock(message_id=7)]
    )
    ok = await adapter.send_message("123", _message())
    assert ok is True
    assert adapter.app.bot.send_message.await_count == 2
    second = adapter.app.bot.send_message.await_args_list[1].kwargs
    assert "parse_mode" not in second
    assert "Title" in second["text"]
    assert "https://example.com/a" in second["text"]


async def test_other_bad_request_still_returns_false():
    """Non-entity BadRequests keep the transient-failure contract
    (False → retry next cycle)."""
    adapter = _adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=BadRequest("Flood control exceeded"))
    ok = await adapter.send_message("123", _message())
    assert ok is False


def test_plain_fallback_always_fits_cap():
    adapter = _adapter()
    msg = _message(title="T" * 1024, summary="S" * 1024, link="https://e.com/" + "x" * 2000)
    assert len(adapter._format_message_plain(msg)) <= 4096
