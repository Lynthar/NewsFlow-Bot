"""Per-feed display controls (show_summary / show_image).

These columns existed since the schema's beginning but nothing consumed
them — this pins the wiring: service setter, dispatcher message shaping
(compact title-only mode, image suppression), and the flags' default-on
behavior for legacy rows.
"""

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.subscription_service import SubscriptionService


async def _feed_with_entry(session) -> tuple[Feed, FeedEntry]:
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    entry = FeedEntry(
        feed_id=feed.id,
        guid="e1",
        title="Big news",
        summary="A long and detailed summary of the big news that happened today",
        content=None,
        link="https://example.com/e1",
        image_url="https://example.com/pic.jpg",
    )
    session.add(entry)
    await session.commit()
    return feed, entry


def _sub(feed: Feed, **overrides) -> Subscription:
    defaults = dict(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        target_language="en",
    )
    defaults.update(overrides)
    return Subscription(**defaults)


async def test_summary_hidden_yields_title_only_message(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, show_summary=False, show_image=True)
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.title == "Big news"
    assert message.summary == ""
    assert message.summary_translated is None
    assert message.image_url == "https://example.com/pic.jpg"


async def test_image_hidden_drops_image_url(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed, show_summary=True, show_image=False)
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.image_url is None
    assert "summary" in message.summary  # untouched


async def test_defaults_show_everything(session):
    feed, entry = await _feed_with_entry(session)
    sub = _sub(feed)
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entry, sub, session)

    assert message.summary != ""
    assert message.image_url == "https://example.com/pic.jpg"


async def test_set_feed_display_service_roundtrip(session):
    feed, _entry = await _feed_with_entry(session)
    sub = _sub(feed)
    session.add(sub)
    await session.commit()

    service = SubscriptionService(session)
    result = await service.set_feed_display(
        platform="discord", channel_id="c", feed_url=feed.url, show_summary=False
    )
    await session.commit()
    assert result.success is True
    assert "summary hidden" in result.message

    await session.refresh(sub)
    assert sub.show_summary is False
    assert sub.show_image is True  # untouched

    # Unknown feed still reports cleanly.
    missing = await service.set_feed_display(
        platform="discord", channel_id="c", feed_url="https://nope/", show_image=False
    )
    assert missing.success is False
