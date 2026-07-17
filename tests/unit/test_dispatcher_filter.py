"""Integration test: dispatcher honors subscription filter rules.

Entries matched out by the filter must:
- NOT be passed to the adapter (no send_message call)
- Be marked in SentEntry with was_filtered=True so they aren't re-evaluated
"""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.services.dispatcher import Dispatcher
from sqlalchemy import select


def _dispatcher_with_adapter(platform: str, adapter) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = platform == "discord"
    fake.telegram_enabled = platform == "telegram"
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    fake.data_dir = MagicMock()
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        d = Dispatcher()
    d.register_adapter(platform, adapter)
    return d


async def test_filter_drops_non_matching_entry(session):
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    for guid, title in [("a", "Python release"), ("b", "JavaScript news")]:
        session.add(
            FeedEntry(
                feed_id=feed.id,
                guid=guid,
                title=title,
                link=f"https://example.com/{guid}",
            )
        )
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        filter_rule={"include_keywords": ["Python"], "exclude_keywords": []},
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    d = _dispatcher_with_adapter("discord", adapter)

    from newsflow.repositories.subscription_repository import SubscriptionRepository

    sub_repo = SubscriptionRepository(session)
    sent_count = await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    # Only the Python entry was delivered.
    assert sent_count == 1
    assert adapter.send_message.await_count == 1

    # Both entries have SentEntry rows, but b is was_filtered=True.
    # Post-2026-05-08 schema: SentEntry stores guid directly, no FK to FeedEntry.
    rows = (
        await session.execute(
            select(SentEntry).where(SentEntry.subscription_id == sub.id)
        )
    ).scalars().all()
    by_guid = {row.guid: row for row in rows}

    assert set(by_guid.keys()) == {"a", "b"}
    assert by_guid["a"].was_filtered is False
    assert by_guid["b"].was_filtered is True


async def test_empty_filter_passes_everything(session):
    """Baseline: subscription without filter_rule behaves like before."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="a",
            title="Any title",
            link="https://example.com/a",
        )
    )
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        filter_rule=None,
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)

    d = _dispatcher_with_adapter("discord", adapter)

    from newsflow.repositories.subscription_repository import SubscriptionRepository

    sub_repo = SubscriptionRepository(session)
    sent_count = await d._dispatch_to_subscription(session, sub, sub_repo)

    assert sent_count == 1
    assert adapter.send_message.await_count == 1


async def test_filter_matches_cleaned_text_not_raw_markup(session):
    """An exclude word living only inside HTML markup (an href) must not
    fire anymore: matching runs on the cleaned text, so URLs and tag
    attributes are invisible to filters."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="a",
            title="Daily roundup",
            summary='<a href="https://adservice.example.com/x">Read more</a>',
            link="https://example.com/a",
        )
    )
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        filter_rule={"include_keywords": [], "exclude_keywords": ["adservice"]},
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    d = _dispatcher_with_adapter("discord", adapter)

    from newsflow.repositories.subscription_repository import SubscriptionRepository

    sent_count = await d._dispatch_to_subscription(session, sub, SubscriptionRepository(session))
    await session.commit()

    assert sent_count == 1  # not excluded — "adservice" only exists in markup


async def test_filter_sees_article_content_field(session):
    """An include keyword appearing only in `content` (not title/summary)
    now matches — the article body used to be invisible to filters."""
    feed = Feed(url="https://example.com/feed", title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    session.add(
        FeedEntry(
            feed_id=feed.id,
            guid="a",
            title="Weekly roundup",
            summary="Assorted items",
            content="<p>A deep dive into quantum computing this week.</p>",
            link="https://example.com/a",
        )
    )
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        filter_rule={"include_keywords": ["quantum"], "exclude_keywords": []},
    )
    session.add(sub)
    await session.commit()

    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value=True)
    adapter.send_text = AsyncMock(return_value=True)
    adapter.is_connected = MagicMock(return_value=True)
    d = _dispatcher_with_adapter("discord", adapter)

    from newsflow.repositories.subscription_repository import SubscriptionRepository

    sent_count = await d._dispatch_to_subscription(session, sub, SubscriptionRepository(session))
    await session.commit()

    assert sent_count == 1
