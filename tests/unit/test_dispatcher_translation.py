"""Regression tests for the translation DB cache in _translate_entry.

A partial translation (title succeeds but the summary call fails) must NOT be
frozen into the FeedEntry DB cache. If it were, the early-cache check would
short-circuit every future dispatch and the summary would never be retried —
the user would keep seeing an untranslated summary even after the provider
recovered. A fully successful translation must still be cached.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.models.feed import Feed, FeedEntry
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.translation.base import TranslationResult


def _dispatcher() -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    fake.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


async def _make_entry(session) -> FeedEntry:
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    entry = FeedEntry(
        feed_id=feed.id,
        guid="g1",
        title="Hello",
        summary="World body text",
        link="https://example.com/g1",
    )
    session.add(entry)
    await session.commit()
    return entry


async def test_partial_translation_is_not_cached(session):
    entry = await _make_entry(session)

    def fake_translate(text, target_lang, source_lang=None):
        if text == "Hello":  # title succeeds
            return TranslationResult(success=True, translated_text="你好")
        return TranslationResult(success=False, error="boom")  # summary fails

    fake_service = MagicMock()
    fake_service.translate = AsyncMock(side_effect=fake_translate)

    d = _dispatcher()
    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        title_t, summary_t = await d._translate_entry(
            entry, "zh-CN", session, "World body text"
        )
    await session.commit()

    assert title_t == "你好"
    assert summary_t is None  # summary failed this round

    # The partial result must NOT be frozen into the cache, so a later
    # dispatch can retry the summary once the provider recovers.
    await session.refresh(entry)
    assert entry.translation_language is None
    assert not entry.title_translated
    assert not entry.summary_translated


async def test_full_translation_is_cached(session):
    entry = await _make_entry(session)

    def fake_translate(text, target_lang, source_lang=None):
        return TranslationResult(
            success=True,
            translated_text="你好" if text == "Hello" else "世界正文",
        )

    fake_service = MagicMock()
    fake_service.translate = AsyncMock(side_effect=fake_translate)

    d = _dispatcher()
    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        title_t, summary_t = await d._translate_entry(
            entry, "zh-CN", session, "World body text"
        )
    await session.commit()

    assert title_t == "你好"
    assert summary_t == "世界正文"

    await session.refresh(entry)
    assert entry.translation_language == "zh-CN"
    assert entry.title_translated == "你好"
    assert entry.summary_translated == "世界正文"
