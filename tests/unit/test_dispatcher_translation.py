"""Regression tests for the translation DB cache in _translate_entry.

A partial translation (title succeeds but the summary call fails) must NOT be
frozen into the FeedEntry DB cache. If it were, the early-cache check would
short-circuit every future dispatch and the summary would never be retried —
the user would keep seeing an untranslated summary even after the provider
recovered. A fully successful translation must still be cached.

Also pins the read side: the cache lives on the shared FeedEntry, so a
subscription with translate=False (or a different target language) must never
receive a translation another subscription cached.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
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


def _sub(feed_id: int, *, translate: bool, language: str) -> Subscription:
    """Transient subscription — _create_message only reads attributes."""
    return Subscription(
        platform="discord",
        platform_user_id="u1",
        platform_channel_id="c1",
        feed_id=feed_id,
        translate=translate,
        target_language=language,
    )


async def _entry_with_cached_zh(session) -> FeedEntry:
    entry = await _make_entry(session)
    entry.title_translated = "你好"
    entry.summary_translated = "世界正文"
    entry.translation_language = "zh-CN"
    await session.commit()
    return entry


async def test_translate_off_ignores_cached_translation(session):
    """A channel that turned translation off gets the original, even when
    another subscription already cached a translation on the entry."""
    entry = await _entry_with_cached_zh(session)
    d = _dispatcher()

    msg = await d._create_message(
        entry, _sub(entry.feed_id, translate=False, language="zh-CN"), session
    )

    assert msg.title_translated is None
    assert msg.summary_translated is None
    assert msg.display_title == "Hello"


async def test_translate_on_reuses_matching_cache_without_api_call(session):
    entry = await _entry_with_cached_zh(session)
    d = _dispatcher()
    fake_service = MagicMock()
    fake_service.translate = AsyncMock()

    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        msg = await d._create_message(
            entry, _sub(entry.feed_id, translate=True, language="zh-CN"), session
        )

    assert msg.title_translated == "你好"
    fake_service.translate.assert_not_called()


async def test_translate_on_ignores_cache_for_other_language(session):
    """Cache holds zh-CN; a ja-targeting subscription must retranslate, not
    inherit the zh-CN text."""
    entry = await _entry_with_cached_zh(session)
    d = _dispatcher()
    fake_service = MagicMock()
    fake_service.translate = AsyncMock(
        return_value=TranslationResult(success=True, translated_text="こんにちは")
    )

    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        msg = await d._create_message(
            entry, _sub(entry.feed_id, translate=True, language="ja"), session
        )

    assert msg.title_translated == "こんにちは"
    assert fake_service.translate.await_count > 0


# ===== same-language short-circuits =====


async def _make_zh_entry(session) -> FeedEntry:
    feed = Feed(url="https://example.com/zhfeed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    entry = FeedEntry(
        feed_id=feed.id,
        guid="zh1",
        title="苹果公司今日发布全新产品线",
        summary="定价策略引发市场热议,这次没有意外",
        link="https://example.com/zh1",
    )
    session.add(entry)
    await session.commit()
    return entry


async def test_script_shortcut_skips_provider_entirely(session):
    """Chinese entry + zh-CN target → zero provider calls, originals used."""
    entry = await _make_zh_entry(session)
    fake_service = MagicMock()
    fake_service.translate = AsyncMock()

    d = _dispatcher()
    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        title_t, summary_t = await d._translate_entry(
            entry, "zh-CN", session, entry.summary
        )

    assert (title_t, summary_t) == (None, None)
    fake_service.translate.assert_not_awaited()


async def test_script_shortcut_respects_variant_boundary(session):
    """Simplified entry + zh-TW target must still hit the provider."""
    entry = await _make_zh_entry(session)

    def fake_translate(text, target_lang, source_lang=None):
        return TranslationResult(success=True, translated_text=f"譯:{text}")

    fake_service = MagicMock()
    fake_service.translate = AsyncMock(side_effect=fake_translate)

    d = _dispatcher()
    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        title_t, summary_t = await d._translate_entry(
            entry, "zh-TW", session, entry.summary
        )

    assert title_t and title_t.startswith("譯:")
    assert fake_service.translate.await_count == 2


async def test_provider_detected_same_language_skips_summary_and_caches(session):
    """Provider detects EN == en target: drop the identity translation,
    skip the summary call, cache originals so later same-target
    subscriptions short-circuit at the top check."""
    entry = await _make_entry(session)  # English title/summary

    fake_service = MagicMock()
    fake_service.translate = AsyncMock(
        return_value=TranslationResult(
            success=True, translated_text="Hello.", source_language="EN"
        )
    )

    d = _dispatcher()
    with patch(
        "newsflow.services.dispatcher.get_translation_service",
        return_value=fake_service,
    ):
        title_t, summary_t = await d._translate_entry(
            entry, "en", session, "World body text"
        )
    await session.commit()

    assert (title_t, summary_t) == (None, None)
    fake_service.translate.assert_awaited_once()  # summary call skipped

    # Originals were cached as the "translation" → the next dispatch's
    # top check returns them without any provider call.
    await session.refresh(entry)
    assert entry.translation_language == "en"
    assert entry.title_translated == "Hello"
    assert entry.summary_translated == "World body text"


async def test_provider_detection_never_shortcuts_zh(session):
    """Detectors report bare ZH, which can't see the simplified↔
    traditional boundary — the zh path must keep translating."""
    entry = await _make_entry(session)  # latin text, script check won't fire

    def fake_translate(text, target_lang, source_lang=None):
        return TranslationResult(
            success=True, translated_text=f"译:{text}", source_language="ZH"
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

    assert title_t == "译:Hello"
    assert summary_t == "译:World body text"
    assert fake_service.translate.await_count == 2
