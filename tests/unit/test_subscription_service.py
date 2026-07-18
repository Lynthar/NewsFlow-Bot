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
        platform="discord",
        channel_id="c1",
        feed_url=FEED_URL,
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
    result = await svc.resume_subscription(platform="discord", channel_id="c1", feed_url=FEED_URL)

    assert result.success is True
    assert sub.is_active is True


async def test_resume_revives_auto_disabled_feed(session):
    """The deactivation notice tells users resume re-enables the source —
    so resume must reset the Feed's error state, not just the subscription
    flag (fetch skips inactive feeds, so it can never self-heal)."""
    sub = await _seed_sub(session)
    sub.is_active = False
    feed = await session.get(Feed, sub.feed_id)
    feed.is_active = False
    feed.error_count = 10
    feed.last_error = "HTTP 500"
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_subscription(platform="discord", channel_id="c1", feed_url=FEED_URL)

    assert result.success is True
    assert sub.is_active is True
    assert feed.is_active is True
    assert feed.error_count == 0
    assert "re-enabled" in result.message


async def test_resume_leaves_healthy_feed_untouched(session):
    """Resuming a paused sub on a healthy feed must not fabricate the
    're-enabled' notice or clobber live error-tracking state."""
    sub = await _seed_sub(session)
    sub.is_active = False
    feed = await session.get(Feed, sub.feed_id)
    feed.error_count = 3  # transient errors, still active and backing off
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_subscription(platform="discord", channel_id="c1", feed_url=FEED_URL)

    assert result.success is True
    assert feed.error_count == 3
    assert "re-enabled" not in result.message


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
        (await session.execute(select(Subscription).order_by(Subscription.feed_id))).scalars().all()
    )
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


async def test_set_feed_filter_persists_keywords(session):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.set_feed_filter(
        platform="discord",
        channel_id="c",
        feed_url="https://example.com/feed",
        include_keywords=("python", "rust"),
        exclude_keywords=("job",),
    )

    assert result.success is True
    await session.refresh(sub)
    assert sub.filter_rule == {
        "include_keywords": ["python", "rust"],
        "exclude_keywords": ["job"],
    }


async def test_set_feed_filter_with_empty_lists_clears(session):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    sub = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        filter_rule={"include_keywords": ["old"], "exclude_keywords": []},
    )
    session.add(sub)
    await session.flush()

    svc = SubscriptionService(session)
    await svc.set_feed_filter(
        platform="discord",
        channel_id="c",
        feed_url="https://example.com/feed",
    )

    await session.refresh(sub)
    assert sub.filter_rule is None


async def test_get_feed_filter_returns_rule_object(session):
    feed = Feed(url="https://example.com/feed", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id="c",
            feed_id=feed.id,
            is_active=True,
            filter_rule={"include_keywords": ["a"], "exclude_keywords": ["b"]},
        )
    )
    await session.flush()

    svc = SubscriptionService(session)
    rule = await svc.get_feed_filter(
        platform="discord", channel_id="c", feed_url="https://example.com/feed"
    )

    assert rule is not None
    assert rule.include_keywords == ("a",)
    assert rule.exclude_keywords == ("b",)


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


async def test_set_feed_silent_toggles(session):
    sub = await _seed_sub(session)
    svc = SubscriptionService(session)

    result = await svc.set_feed_silent(
        platform="discord",
        channel_id="c1",
        feed_url=FEED_URL,
        silent=True,
    )

    assert result.success is True
    await session.refresh(sub)
    assert sub.silent is True


async def test_set_feed_silent_unknown_feed_fails(session):
    svc = SubscriptionService(session)
    result = await svc.set_feed_silent(
        platform="discord",
        channel_id="c1",
        feed_url="https://nope.example/feed",
        silent=True,
    )
    assert result.success is False
    assert "not found" in result.message.lower()


async def test_set_channel_silent_bulk_toggles(session):
    """All subs in channel get flipped; result.message reports the count."""
    feed_a = Feed(url="https://example.com/a")
    feed_b = Feed(url="https://example.com/b")
    session.add_all([feed_a, feed_b])
    await session.flush()
    for f in (feed_a, feed_b):
        session.add(
            Subscription(
                platform="discord",
                platform_user_id="u",
                platform_channel_id="c1",
                feed_id=f.id,
                is_active=True,
            )
        )
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.set_channel_silent(platform="discord", channel_id="c1", silent=True)

    assert result.success is True
    assert "2 subscription" in result.message


async def test_set_channel_silent_no_op_when_already_in_state(session):
    """Empty channel (or all-already-target) returns success, flips
    nothing, and records the preference as the channel default so
    future subscriptions inherit it."""
    svc = SubscriptionService(session)
    result = await svc.set_channel_silent(platform="discord", channel_id="empty", silent=True)
    assert result.success is True
    assert "default" in result.message.lower()
    defaults = await svc.channel_settings_repo.get("discord", "empty")
    assert defaults is not None
    assert defaults.default_silent is True


# ===== silent inheritance on subscribe =====


async def test_channel_silent_default_empty_channel(session):
    """Empty channel → False. Documented limitation: a lone /silent on
    on an empty channel doesn't carry over to the first /add."""
    svc = SubscriptionService(session)
    assert await svc._channel_silent_default("discord", "empty") is False


async def test_channel_silent_default_all_silent(session):
    feed_a = Feed(url="https://example.com/a")
    feed_b = Feed(url="https://example.com/b")
    session.add_all([feed_a, feed_b])
    await session.flush()
    for f in (feed_a, feed_b):
        session.add(
            Subscription(
                platform="discord",
                platform_user_id="u",
                platform_channel_id="c1",
                feed_id=f.id,
                is_active=True,
                silent=True,
            )
        )
    await session.flush()

    svc = SubscriptionService(session)
    assert await svc._channel_silent_default("discord", "c1") is True


async def test_channel_silent_default_mixed_returns_false(session):
    """Even one non-silent sub in the channel disables inheritance."""
    feed_a = Feed(url="https://example.com/a")
    feed_b = Feed(url="https://example.com/b")
    session.add_all([feed_a, feed_b])
    await session.flush()
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id="c1",
            feed_id=feed_a.id,
            is_active=True,
            silent=True,
        )
    )
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id="c1",
            feed_id=feed_b.id,
            is_active=True,
            silent=False,
        )
    )
    await session.flush()

    svc = SubscriptionService(session)
    assert await svc._channel_silent_default("discord", "c1") is False


async def test_management_commands_resolve_source_shortcut(session):
    """A feed added via a shortcut (gh:owner/repo) is stored under the expanded
    URL. Management commands must resolve the same shortcut, not fail with
    'Feed not found'. Regression for the add-expands / manage-doesn't asymmetry.
    """
    expanded = "https://github.com/owner/repo/releases.atom"  # gh: expansion
    feed = Feed(url=expanded, title="repo", is_active=True, error_count=0)
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

    # pause via the shortcut form resolves to the stored feed
    paused = await svc.pause_subscription(
        platform="discord", channel_id="c", feed_url="gh:owner/repo"
    )
    assert paused.success is True
    await session.refresh(sub)
    assert sub.is_active is False

    # so do the other lookups (silent / detail), all keyed off the shortcut
    silenced = await svc.set_feed_silent(
        platform="discord", channel_id="c", feed_url="gh:owner/repo", silent=True
    )
    assert silenced.success is True

    detail = await svc.get_subscription_detail(
        platform="discord", channel_id="c", feed_url="gh:owner/repo"
    )
    assert detail is not None and detail.feed.id == feed.id

    # and unsubscribe finally removes it via the shortcut
    removed = await svc.unsubscribe(platform="discord", channel_id="c", feed_url="gh:owner/repo")
    assert removed.success is True


async def test_channel_silent_default_paused_subs_excluded(session):
    """Paused (is_active=False) subs don't participate in the check —
    `get_channel_subscriptions` already filters them out, so a channel
    with only paused subs is treated as empty (returns False)."""
    feed = Feed(url="https://example.com/a")
    session.add(feed)
    await session.flush()
    session.add(
        Subscription(
            platform="discord",
            platform_user_id="u",
            platform_channel_id="c1",
            feed_id=feed.id,
            is_active=False,  # paused
            silent=True,
        )
    )
    await session.flush()

    svc = SubscriptionService(session)
    assert await svc._channel_silent_default("discord", "c1") is False


# ── F10: paused subscriptions stay visible / bulk resume ─────────────────────


async def _seed_two_subs(session):
    """One active + one paused subscription in channel c1."""
    feed_a = Feed(url="https://example.com/a", title="A", is_active=True)
    feed_b = Feed(url="https://example.com/b", title="B", is_active=True)
    session.add_all([feed_a, feed_b])
    await session.flush()
    sub_a = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c1",
        feed_id=feed_a.id,
        is_active=True,
    )
    sub_b = Subscription(
        platform="discord",
        platform_user_id="u",
        platform_channel_id="c1",
        feed_id=feed_b.id,
        is_active=False,  # paused
    )
    session.add_all([sub_a, sub_b])
    await session.flush()
    return sub_a, sub_b, feed_a, feed_b


async def test_get_channel_subscriptions_lists_paused_when_asked(session):
    """Pausing must not make a subscription unfindable — /feed list and
    export pass include_inactive=True; dispatch-facing callers keep the
    active-only default."""
    sub_a, sub_b, *_ = await _seed_two_subs(session)
    svc = SubscriptionService(session)

    active_only = await svc.get_channel_subscriptions("discord", "c1")
    assert [s.id for s in active_only] == [sub_a.id]

    everything = await svc.get_channel_subscriptions("discord", "c1", include_inactive=True)
    assert [s.id for s in everything] == [sub_a.id, sub_b.id]  # ordered by id


async def test_export_opml_includes_paused_subscriptions(session):
    """An export is a backup — dropping paused feeds would lose them."""
    await _seed_two_subs(session)
    svc = SubscriptionService(session)

    xml = await svc.export_opml("discord", "c1")

    assert "https://example.com/a" in xml
    assert "https://example.com/b" in xml


async def test_update_settings_reaches_paused_subscriptions(session):
    """Channel-wide settings apply to paused subs too, so they don't resume
    with stale language/translate values later."""
    _, sub_b, *_ = await _seed_two_subs(session)
    svc = SubscriptionService(session)

    updated = await svc.update_settings(platform="discord", channel_id="c1", target_language="ja")

    assert updated == 2  # both subs, paused included
    assert sub_b.target_language == "ja"


async def test_resume_all_reactivates_subs_and_revives_feeds(session):
    sub_a, sub_b, feed_a, feed_b = await _seed_two_subs(session)
    # The paused sub's feed was also auto-disabled while nobody watched.
    feed_b.is_active = False
    feed_b.error_count = 10
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_all_subscriptions("discord", "c1")

    assert result.success is True
    assert sub_b.is_active is True
    assert feed_b.is_active is True
    assert feed_b.error_count == 0
    assert "1 subscription" in result.message
    assert "1 auto-disabled feed" in result.message
    # The already-active sub and healthy feed are untouched.
    assert sub_a.is_active is True and feed_a.is_active is True


async def test_resume_all_reports_nothing_to_do(session):
    sub_a, sub_b, *_ = await _seed_two_subs(session)
    sub_b.is_active = True
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_all_subscriptions("discord", "c1")

    assert result.success is True
    assert "already active" in result.message


async def test_resume_all_empty_channel_fails(session):
    svc = SubscriptionService(session)
    result = await svc.resume_all_subscriptions("discord", "empty-channel")
    assert result.success is False


async def test_resume_all_notes_disabled_digest(session):
    """ChannelGone deactivation also disables the channel digest, but
    resume-all can't re-enable it blindly (a manual /digest disable looks
    identical) — so the reply must at least say it's still off."""
    from newsflow.models.digest import ChannelDigest

    await _seed_two_subs(session)
    session.add(ChannelDigest(platform="discord", platform_channel_id="c1", enabled=False))
    await session.flush()

    svc = SubscriptionService(session)
    result = await svc.resume_all_subscriptions("discord", "c1")

    assert result.success is True
    assert "digest is still disabled" in result.message


async def test_resume_all_no_digest_note_when_absent(session):
    await _seed_two_subs(session)

    svc = SubscriptionService(session)
    result = await svc.resume_all_subscriptions("discord", "c1")

    assert result.success is True
    assert "digest" not in result.message
