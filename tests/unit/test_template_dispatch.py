"""Message templates at the dispatch/service layer.

Pins the load-bearing semantics: rendering happens BEFORE the display
trims (so {summary}/{image_url} always resolve while show_image still
governs the platform attachment), a template that renders to nothing or
crashes falls back to the default layout (an article must never be
lost), stored-but-stale placeholders fail open, and the service
setter/bulk/clear round-trips.
"""

from datetime import UTC, datetime

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.subscription_service import SubscriptionService


async def _feed_with_entry(session, **entry_overrides) -> tuple[Feed, FeedEntry]:
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    fields = dict(
        feed_id=feed.id,
        guid="e1",
        title="Big news",
        summary="A long and detailed summary of the big news that happened today",
        content=None,
        link="https://example.com/e1",
        image_url="https://example.com/pic.jpg",
    )
    fields.update(entry_overrides)
    entry = FeedEntry(**fields)
    session.add(entry)
    await session.commit()
    return feed, entry


def _sub(feed: Feed, **overrides) -> Subscription:
    defaults = dict(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        target_language="en",
    )
    defaults.update(overrides)
    return Subscription(**defaults)


# ------------------------------------------------------- dispatcher wiring


async def test_template_renders_into_message(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, message_template="📌 {title}\n{summary}\n🔗 {url}")
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.template_text == f"📌 Big news\n{message.summary}\n🔗 https://example.com/e1"
    # Default fields still populated — adapters without template support
    # (and the fallback path) keep working.
    assert message.title == "Big news"


async def test_no_template_leaves_template_text_none(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed)
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.template_text is None


async def test_template_ignores_show_summary_but_trims_message_fields(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, show_summary=False, message_template="{title}|{summary}")
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    # Template gets the pre-trim summary; the Message field is trimmed.
    assert "summary of the big news" in (message.template_text or "")
    assert message.summary == ""


async def test_template_resolves_image_while_attachment_suppressed(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, show_image=False, message_template="{image_url}")
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.template_text == "https://example.com/pic.jpg"
    assert message.image_url is None  # attachment still governed by show_image


async def test_template_rendering_to_nothing_falls_back(session):
    feed, entry = await _feed_with_entry(session, image_url=None)
    sub = _sub(feed, message_template="🖼 {image_url}")
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    # Whole template collapsed (only-empty-placeholder line) → default layout.
    assert message.template_text is None


async def test_template_render_crash_falls_back(session, monkeypatch):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, message_template="{title}")
    session.add(sub)
    await session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("newsflow.services.dispatcher.render_template", _boom)

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.template_text is None
    assert message.title == "Big news"


async def test_stored_unknown_placeholder_fails_open(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, message_template="{title} {bogus}")
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.template_text == "Big news {bogus}"


async def test_template_uses_cached_translation(session):
    feed, entry = await _feed_with_entry(
        session,
        title_translated="大新闻",
        summary_translated="今天发生的大新闻的详细摘要",
        translation_language="zh-CN",
    )
    sub = _sub(
        feed,
        translate=True,
        target_language="zh-CN",
        message_template="{title} / {original_title}",
    )
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    # Bilingual layout: effective title is the cached translation, the
    # original stays reachable — no provider call involved.
    assert message.template_text == "大新闻 / Big news"


# ------------------------------------------------------ service round-trip


async def test_set_feed_template_roundtrip_and_clear(session):
    feed, _entry = await _feed_with_entry(session)
    sub = _sub(feed)
    session.add(sub)
    await session.commit()

    service = SubscriptionService(session)
    result = await service.set_feed_template(
        platform="telegram", channel_id="c", feed_url=feed.url, template="{title}"
    )
    await session.commit()
    assert result.success is True
    await session.refresh(sub)
    assert sub.message_template == "{title}"

    cleared = await service.set_feed_template(
        platform="telegram", channel_id="c", feed_url=feed.url, template=None
    )
    await session.commit()
    assert cleared.success is True
    assert "cleared" in cleared.message
    await session.refresh(sub)
    assert sub.message_template is None

    missing = await service.set_feed_template(
        platform="telegram", channel_id="c", feed_url="https://nope/", template="{title}"
    )
    assert missing.success is False


async def test_set_channel_template_bulk_hits_paused_and_spares_others(session):
    feed, _entry = await _feed_with_entry(session)
    feed2 = Feed(url="https://example.com/feed2", title="Two", is_active=True, error_count=0)
    session.add(feed2)
    await session.flush()
    active = _sub(feed)
    paused = _sub(feed2, is_active=False)
    other_channel = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="other",
        feed_id=feed.id,
        is_active=True,
    )
    session.add_all([active, paused, other_channel])
    await session.commit()

    service = SubscriptionService(session)
    count = await service.set_channel_template("telegram", "c", "{title}")
    await session.commit()

    assert count == 2
    for sub in (active, paused):
        await session.refresh(sub)
        assert sub.message_template == "{title}"
    await session.refresh(other_channel)
    assert other_channel.message_template is None


# -------------------------------------------------------- preview builder


def test_preview_uses_cached_translation_when_language_matches():
    entry = FeedEntry(
        feed_id=1,
        guid="g",
        title="Original title",
        summary="<p>Body &amp; more</p>",
        content=None,
        link="https://example.com/a",
        image_url=None,
        title_translated="译标题",
        summary_translated="译摘要",
        translation_language="zh-CN",
        published_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
    )
    out = SubscriptionService.build_template_preview("{title}|{original_title}", entry, "zh-CN")
    assert out == "译标题|Original title"


def test_preview_ignores_cached_translation_on_language_mismatch():
    entry = FeedEntry(
        feed_id=1,
        guid="g",
        title="Original title",
        summary="Body",
        content=None,
        link="https://example.com/a",
        image_url=None,
        title_translated="訳タイトル",
        translation_language="ja",
    )
    out = SubscriptionService.build_template_preview("{title}", entry, "zh-CN")
    assert out == "Original title"


def test_preview_cleans_html_and_falls_back_to_sample():
    entry = FeedEntry(
        feed_id=1,
        guid="g",
        title="T",
        summary="<p>Plain &amp; clean</p>",
        content=None,
        link="https://example.com/a",
        image_url=None,
    )
    out = SubscriptionService.build_template_preview("{summary}", entry, None)
    assert out == "Plain & clean"

    sample = SubscriptionService.build_template_preview("{title} — {url}", None, None)
    assert sample == "Example headline — https://example.com/article"
