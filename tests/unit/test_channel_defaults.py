"""ChannelSettings: persisted per-channel defaults inherited on subscribe.

The channel-wide setting commands now record the preference itself, so a
NEW subscription inherits it — including on channels that had zero
subscriptions when the preference was set (the old all-existing-subs
heuristic could never remember that)."""

from unittest.mock import AsyncMock

from newsflow.core.feed_fetcher import FetchResult
from newsflow.services.subscription_service import SubscriptionService


def _mock_fetch(svc: SubscriptionService, url: str = "https://example.com/feed") -> None:
    svc.feed_service.fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url=url,
            success=True,
            entries=[{"guid": "g1", "title": "T1", "link": "https://x/1"}],
            feed_title="Example",
        )
    )


async def test_new_subscription_inherits_channel_defaults(session):
    svc = SubscriptionService(session)
    _mock_fetch(svc)

    # Preferences recorded on an EMPTY channel — used to be a no-op error.
    updated = await svc.update_settings(
        platform="discord", channel_id="c9", target_language="ja", translate=False
    )
    assert updated == 0  # nothing existed yet; the preference is still saved

    result = await svc.subscribe(
        platform="discord",
        user_id="u",
        channel_id="c9",
        feed_url="https://example.com/feed",
    )
    assert result.success
    assert result.subscription is not None
    assert result.subscription.target_language == "ja"
    assert result.subscription.translate is False


async def test_silent_on_empty_channel_sticks_for_future_subscribe(session):
    """The old heuristic forgot a lone /silent on in an empty channel by
    the time the first /add arrived. The recorded default doesn't."""
    svc = SubscriptionService(session)
    _mock_fetch(svc)

    await svc.set_channel_silent(platform="discord", channel_id="c9", silent=True)

    result = await svc.subscribe(
        platform="discord",
        user_id="u",
        channel_id="c9",
        feed_url="https://example.com/feed",
    )
    assert result.subscription is not None
    assert result.subscription.silent is True


async def test_explicit_default_beats_all_silent_heuristic(session):
    """A recorded default_silent=False wins even when every existing sub
    is silent (which the legacy heuristic reads as 'silent channel')."""
    svc = SubscriptionService(session)
    _mock_fetch(svc)
    r1 = await svc.subscribe(
        platform="discord", user_id="u", channel_id="c9", feed_url="https://example.com/feed"
    )
    assert r1.subscription is not None
    r1.subscription.silent = True
    await session.flush()

    await svc.channel_settings_repo.upsert("discord", "c9", default_silent=False)

    _mock_fetch(svc, url="https://example.com/feed2")
    r2 = await svc.subscribe(
        platform="discord", user_id="u", channel_id="c9", feed_url="https://example.com/feed2"
    )
    assert r2.subscription is not None
    assert r2.subscription.silent is False


async def test_heuristic_still_applies_without_recorded_default(session):
    """No ChannelSettings row → legacy behavior intact: an all-silent
    channel silences the newcomer."""
    svc = SubscriptionService(session)
    _mock_fetch(svc)
    r1 = await svc.subscribe(
        platform="discord", user_id="u", channel_id="c9", feed_url="https://example.com/feed"
    )
    assert r1.subscription is not None
    r1.subscription.silent = True
    await session.flush()

    _mock_fetch(svc, url="https://example.com/feed2")
    r2 = await svc.subscribe(
        platform="discord", user_id="u", channel_id="c9", feed_url="https://example.com/feed2"
    )
    assert r2.subscription is not None
    assert r2.subscription.silent is True


async def test_language_command_repeat_only_touches_its_own_field(session):
    """/silent's default must survive a later /language (partial upsert)."""
    svc = SubscriptionService(session)
    await svc.set_channel_silent(platform="discord", channel_id="c9", silent=True)
    await svc.update_settings(platform="discord", channel_id="c9", target_language="en")

    defaults = await svc.channel_settings_repo.get("discord", "c9")
    assert defaults is not None
    assert defaults.default_silent is True
    assert defaults.default_language == "en"
    assert defaults.default_translate is None
