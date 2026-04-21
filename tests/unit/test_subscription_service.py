"""Tests for SubscriptionService pause/resume/detail."""

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.services.subscription_service import SubscriptionService


FEED_URL = "https://example.com/feed"


async def _seed_sub(session) -> Subscription:
    feed = Feed(url=FEED_URL, title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord",
        platform_user_id="u1",
        platform_channel_id="c1",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()
    return sub


async def test_pause_subscription_marks_inactive(session):
    sub = await _seed_sub(session)
    svc = SubscriptionService(session)

    result = await svc.pause_subscription(
        platform="discord", channel_id="c1", feed_url=FEED_URL,
    )

    assert result.success is True
    assert sub.is_active is False


async def test_pause_subscription_unknown_feed_fails(session):
    svc = SubscriptionService(session)

    result = await svc.pause_subscription(
        platform="discord", channel_id="c1", feed_url="https://nope.example/feed"
    )

    assert result.success is False
    assert "not found" in result.message.lower()


async def test_pause_subscription_unknown_sub_fails(session):
    # Feed exists but no subscription for this channel.
    feed = Feed(url="https://example.com/feed")
    session.add(feed)
    await session.flush()
    svc = SubscriptionService(session)

    result = await svc.pause_subscription(
        platform="discord", channel_id="other-channel", feed_url=feed.url
    )

    assert result.success is False


async def test_resume_subscription_reactivates(session):
    sub = await _seed_sub(session)
    sub.is_active = False
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_subscription(
        platform="discord", channel_id="c1", feed_url=FEED_URL
    )

    assert result.success is True
    assert sub.is_active is True


async def test_get_subscription_detail_returns_sub_and_recent_entries(session):
    sub = await _seed_sub(session)
    # Add 3 entries so detail has content.
    for i in range(3):
        session.add(
            FeedEntry(
                feed_id=sub.feed_id,
                guid=f"g{i}",
                title=f"Entry {i}",
                link=f"https://example.com/{i}",
            )
        )
    await session.flush()

    svc = SubscriptionService(session)
    detail = await svc.get_subscription_detail(
        platform="discord", channel_id="c1", feed_url=FEED_URL, entry_limit=2
    )

    assert detail is not None
    assert detail.subscription.id == sub.id
    assert detail.feed.id == sub.feed_id
    assert len(detail.recent_entries) == 2


async def test_get_subscription_detail_returns_none_for_missing_feed(session):
    svc = SubscriptionService(session)

    detail = await svc.get_subscription_detail(
        platform="discord", channel_id="c1", feed_url="https://nope.example/feed"
    )

    assert detail is None


async def test_set_feed_language_updates_only_named_subscription(session):
    # Two subscriptions in same channel, different feeds.
    feeds = []
    for url, title in [
        ("https://a.example/feed", "A"),
        ("https://b.example/feed", "B"),
    ]:
        f = Feed(url=url, title=title, is_active=True, error_count=0)
        session.add(f)
        await session.flush()
        feeds.append(f)
        session.add(
            Subscription(
                platform="discord",
                platform_user_id="u",
                platform_channel_id="c",
                feed_id=f.id,
                is_active=True,
                target_language="zh-CN",
            )
        )
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.set_feed_language(
        platform="discord",
        channel_id="c",
        feed_url="https://a.example/feed",
        language="ja",
    )

    assert result.success is True

    # Re-query to see the update.
    from sqlalchemy import select

    rows = (
        await session.execute(
            select(Subscription).order_by(Subscription.feed_id)
        )
    ).scalars().all()
    assert rows[0].target_language == "ja"  # A updated
    assert rows[1].target_language == "zh-CN"  # B untouched


async def test_set_feed_translate_toggles_only_named_subscription(session):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=True,
    )
    session.add(sub)
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.set_feed_translate(
        platform="discord",
        channel_id="c",
        feed_url="https://example.com/feed",
        enabled=False,
    )

    assert result.success is True
    await session.refresh(sub)
    assert sub.translate is False


async def test_export_opml_produces_parseable_document(session):
    feed = Feed(
        url="https://example.com/feed",
        title="Example",
        site_url="https://example.com",
    )
    session.add(feed)
    await session.flush()
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id="c",
            feed_id=feed.id,
            is_active=True,
        )
    )
    await session.flush()

    svc = SubscriptionService(session)
    xml = await svc.export_opml("discord", "c")

    from newsflow.core.opml import parse_opml

    entries = parse_opml(xml)
    assert len(entries) == 1
    assert entries[0].url == "https://example.com/feed"
    assert entries[0].title == "Example"
    assert entries[0].html_url == "https://example.com"


async def test_import_opml_bulk_subscribes(session):
    from unittest.mock import AsyncMock

    from newsflow.core.feed_fetcher import FetchResult

    svc = SubscriptionService(session)
    svc.feed_service.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="placeholder",
            success=True,
            entries=[{"guid": "g", "title": "T", "link": "https://x"}],
            feed_title="Fetched",
        )
    )

    opml_doc = """<opml><body>
        <outline type="rss" xmlUrl="https://a.example/feed"/>
        <outline type="rss" xmlUrl="https://b.example/feed"/>
    </body></opml>"""

    result = await svc.import_opml(
        platform="discord",
        user_id="u",
        channel_id="c",
        opml_content=opml_doc,
    )

    assert sorted(result.added) == [
        "https://a.example/feed",
        "https://b.example/feed",
    ]
    assert result.already_subscribed == []
    assert result.failed == []


async def test_import_opml_reports_parse_errors(session):
    svc = SubscriptionService(session)

    result = await svc.import_opml(
        platform="discord",
        user_id="u",
        channel_id="c",
        opml_content="<opml><body><outline text='none'/></body></opml>",
    )

    assert result.added == []
    assert len(result.failed) == 1
    assert result.failed[0][0] == "<opml>"
