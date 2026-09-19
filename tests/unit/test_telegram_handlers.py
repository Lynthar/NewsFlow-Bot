"""Telegram handler-level tests: HTML escaping and the global error handler.

The /add confirmation interpolates feed titles and URLs into a
parse_mode="HTML" message. Feed titles routinely contain "&" (AT&T,
feedparser-decoded entities) and URLs carry query strings — unescaped,
Telegram rejects the edit and the user is stuck on "Adding feed..."
forever even though the subscription actually succeeded. These tests
drive the real handler against the real database with the feed fetch
stubbed at the HTTP boundary, plus the _on_error contract (user always
gets an acknowledgement, handler never raises).
"""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.telegram.bot import _on_error, add_command
from newsflow.core.feed_fetcher import FetchResult


def _update_with_processing_msg():
    processing_msg = MagicMock()
    processing_msg.edit_text = AsyncMock()
    update = MagicMock()
    update.message.reply_text = AsyncMock(return_value=processing_msg)
    update.effective_chat.id = -100123
    # Private chat: the group-admin gate never applies (its behavior is
    # pinned separately in test_permissions.py; these tests pin escaping).
    update.effective_chat.type = "private"
    update.effective_user.id = 42
    update.message.is_topic_message = False  # a MagicMock here would be stored as the topic
    return update, processing_msg


async def _run_add(monkeypatch, fetched: FetchResult, url: str) -> str:
    fetcher = MagicMock()
    fetcher.fetch_feed = AsyncMock(return_value=fetched)
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)
    update, processing_msg = _update_with_processing_msg()
    context = MagicMock()
    context.args = [url]
    dispatcher = MagicMock()
    dispatcher.spawn = MagicMock()
    dispatcher.schedule_preview = MagicMock(return_value=MagicMock())

    with patch("newsflow.adapters.telegram.bot.get_dispatcher", return_value=dispatcher):
        await add_command(update, context)

    processing_msg.edit_text.assert_awaited_once()
    call = processing_msg.edit_text.call_args
    assert call.kwargs.get("parse_mode") == "HTML"
    return call.args[0]


async def test_add_confirmation_escapes_title_and_url(db, monkeypatch):
    url = "https://ex.com/feed?a=1&b=2"
    fetched = FetchResult(
        url=url,
        success=True,
        entries=[{"guid": "e1", "title": "First", "link": "https://ex.com/1"}],
        feed_title="AT&T <Live> News",
    )

    text = await _run_add(monkeypatch, fetched, url)

    assert "AT&amp;T &lt;Live&gt; News" in text
    assert "a=1&amp;b=2" in text
    # The raw, HTML-breaking forms must be gone.
    assert "AT&T <Live>" not in text
    assert "a=1&b=2" not in text


async def test_add_failure_message_is_escaped(db, monkeypatch):
    url = "https://ex.com/feed?x=1&y=2"
    fetched = FetchResult(url=url, success=False, entries=[], error="404 <not found>")

    text = await _run_add(monkeypatch, fetched, url)

    assert "&lt;not found&gt;" in text
    assert "x=1&amp;y=2" in text


async def test_on_error_replies_with_plain_text_notice():
    update = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()
    context.error = RuntimeError("boom")

    await _on_error(update, context)

    update.effective_message.reply_text.assert_awaited_once()
    # Plain text on purpose — the error path must not be able to fail on
    # markup itself.
    assert "parse_mode" not in update.effective_message.reply_text.call_args.kwargs


async def test_on_error_never_raises_even_if_reply_fails():
    update = MagicMock()
    update.effective_message.reply_text = AsyncMock(side_effect=RuntimeError("network down"))
    context = MagicMock()
    context.error = ValueError("original")

    await _on_error(update, context)  # must not raise


async def test_on_error_tolerates_updateless_invocation():
    """PTB also routes job/queue errors here with update=None-ish objects."""
    context = MagicMock()
    context.error = ValueError("job error")

    await _on_error(object(), context)  # must not raise
