"""Tests for sources.yaml parsing + reconcile.

Covers: parse validation; reconcile create / idempotency / update / removal of
sources and individual subscribers; and the safety guarantee that RSS feeds and
non-owned subscriptions are never touched.
"""

import pytest
from newsflow.models.feed import Feed
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.services.source_sync import (
    SourceCfg,
    SourceConfigError,
    SubscriberCfg,
    _reconcile,
    parse_sources_yaml,
)
from sqlalchemy import select

# ── parsing ──────────────────────────────────────────────────────────────────


def _write(tmp_path, text: str):
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_valid(tmp_path):
    p = _write(
        tmp_path,
        """
sources:
  api1:
    url: https://api.example.com/items
    type: json_api
    config:
      items: "$.data[*]"
      guid: id
    subscribers:
      - platform: discord
        channel: "123"
""",
    )
    srcs = parse_sources_yaml(p)
    assert len(srcs) == 1
    s = srcs[0]
    assert s.name == "api1" and s.type == "json_api"
    assert s.config["items"] == "$.data[*]"
    assert s.subscribers[0].platform == "discord"
    assert s.subscribers[0].channel == "123"


def test_parse_unknown_type_fails(tmp_path):
    # 'rss' is managed elsewhere; sources.yaml only accepts non-RSS types.
    p = _write(tmp_path, "sources:\n  x:\n    url: https://e/x\n    type: rss\n")
    with pytest.raises(SourceConfigError, match="unknown type"):
        parse_sources_yaml(p)


def test_parse_missing_url_fails(tmp_path):
    p = _write(tmp_path, "sources:\n  x:\n    type: json_api\n")
    with pytest.raises(SourceConfigError, match="url"):
        parse_sources_yaml(p)


def test_parse_bad_subscriber_platform_fails(tmp_path):
    p = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        "    subscribers:\n      - platform: irc\n        channel: '1'\n",
    )
    with pytest.raises(SourceConfigError, match="platform"):
        parse_sources_yaml(p)


# ── reconcile ────────────────────────────────────────────────────────────────


def _src(url: str = "https://api.example.com/items", subs=None) -> SourceCfg:
    return SourceCfg(
        name="api1",
        url=url,
        type="json_api",
        config={"items": "$.data[*]", "guid": "id"},
        subscribers=(
            subs
            if subs is not None
            else [SubscriberCfg(platform="discord", channel="123")]
        ),
    )


async def _feeds(session):
    return (await session.execute(select(Feed))).scalars().all()


async def _subs(session):
    return (await session.execute(select(Subscription))).scalars().all()


async def test_reconcile_creates_feed_and_sub(session):
    await _reconcile(session, [_src()])
    await session.commit()

    feeds = await _feeds(session)
    assert len(feeds) == 1
    assert feeds[0].source_type == "json_api"
    assert feeds[0].config == {"items": "$.data[*]", "guid": "id"}

    subs = await _subs(session)
    assert len(subs) == 1
    assert subs[0].platform == "discord"
    assert subs[0].platform_channel_id == "123"
    assert subs[0].platform_user_id == "source-yaml"  # ownership marker


async def test_reconcile_idempotent(session):
    await _reconcile(session, [_src()])
    await session.commit()
    await _reconcile(session, [_src()])
    await session.commit()
    assert len(await _feeds(session)) == 1
    assert len(await _subs(session)) == 1


async def test_reconcile_updates_config_and_sub_settings(session):
    await _reconcile(session, [_src()])
    await session.commit()

    updated = SourceCfg(
        name="api1",
        url="https://api.example.com/items",
        type="json_api",
        config={"items": "$.results[*]", "guid": "uid"},
        subscribers=[
            SubscriberCfg(
                platform="discord", channel="123", translate=True, language="en"
            )
        ],
    )
    await _reconcile(session, [updated])
    await session.commit()

    feeds = await _feeds(session)
    assert feeds[0].config == {"items": "$.results[*]", "guid": "uid"}
    subs = await _subs(session)
    assert subs[0].translate is True and subs[0].target_language == "en"


async def test_reconcile_removes_dropped_source(session):
    await _reconcile(session, [_src()])
    await session.commit()
    await _reconcile(session, [])  # source removed from the file
    await session.commit()
    assert len(await _feeds(session)) == 0
    assert len(await _subs(session)) == 0


async def test_reconcile_removes_dropped_subscriber(session):
    await _reconcile(
        session,
        [
            _src(
                subs=[
                    SubscriberCfg(platform="discord", channel="123"),
                    SubscriberCfg(platform="telegram", channel="456"),
                ]
            )
        ],
    )
    await session.commit()
    assert len(await _subs(session)) == 2

    # Drop the telegram subscriber but keep the source.
    await _reconcile(
        session, [_src(subs=[SubscriberCfg(platform="discord", channel="123")])]
    )
    await session.commit()

    subs = await _subs(session)
    assert len(subs) == 1 and subs[0].platform == "discord"
    assert len(await _feeds(session)) == 1  # source itself kept


async def test_removed_source_keeps_feed_with_foreign_subscribers(session):
    """Dropping a source from the file must not cascade-delete subscriptions
    other owners created on the same feed (interactive /feed add, or
    webhooks.yaml rows) — nor their SentEntry dedupe history. Only the
    source-yaml subscriptions go."""
    await _reconcile(session, [_src()])
    await session.commit()
    feed = (await _feeds(session))[0]

    foreign = Subscription(
        platform="discord",
        platform_user_id="a-real-human",  # not "source-yaml"
        platform_channel_id="999",
        feed_id=feed.id,
        is_active=True,
    )
    session.add(foreign)
    await session.flush()
    session.add(SentEntry(subscription_id=foreign.id, feed_id=feed.id, guid="seen-1"))
    await session.commit()

    await _reconcile(session, [])  # source removed from the file
    await session.commit()

    feeds = await _feeds(session)
    assert len(feeds) == 1  # feed survives for the foreign subscriber
    subs = await _subs(session)
    assert len(subs) == 1 and subs[0].platform_user_id == "a-real-human"
    sent = (await session.execute(select(SentEntry))).scalars().all()
    assert len(sent) == 1 and sent[0].guid == "seen-1"  # dedupe history intact


async def test_reconcile_reactivates_auto_disabled_source_feed(session):
    """A source feed auto-disabled by consecutive fetch errors must come back
    on the next reconcile while still declared in the file — the dispatch loop
    skips inactive feeds, so nothing else can revive it."""
    await _reconcile(session, [_src()])
    await session.commit()
    feed = (await _feeds(session))[0]
    feed.is_active = False
    feed.error_count = 10
    await session.commit()

    await _reconcile(session, [_src()])
    await session.commit()

    feed = (await _feeds(session))[0]
    assert feed.is_active is True
    assert feed.error_count == 0


async def test_reconcile_leaves_rss_feeds_untouched(session):
    rss = Feed(url="https://blog.example.com/rss", source_type="rss")
    session.add(rss)
    await session.commit()

    await _reconcile(session, [_src()])
    await session.commit()
    await _reconcile(session, [])  # remove every declared source
    await session.commit()

    feeds = await _feeds(session)
    assert len(feeds) == 1 and feeds[0].source_type == "rss"  # RSS untouched


async def test_reconcile_skips_url_colliding_with_existing_rss_feed(session):
    """If a sources.yaml URL collides with an interactively-added RSS feed, the
    sync must NOT convert it to json_api / overwrite its config / subscribe to
    it — and must not delete it on a later removal. The whole source is skipped.
    """
    collide_url = "https://api.example.com/items"  # == _src() default url
    rss = Feed(url=collide_url, source_type="rss", title="User's RSS")
    session.add(rss)
    await session.commit()
    rss_id = rss.id

    # Reconcile a source whose URL hits the existing RSS feed.
    await _reconcile(session, [_src()])
    await session.commit()

    feeds = await _feeds(session)
    assert len(feeds) == 1
    assert feeds[0].id == rss_id
    assert feeds[0].source_type == "rss"  # NOT converted
    assert feeds[0].config is None  # config NOT overwritten
    assert len(await _subs(session)) == 0  # no source-yaml sub created

    # Removing every source must leave the user's RSS feed intact (the sync
    # only deletes feeds it actually owns).
    await _reconcile(session, [])
    await session.commit()
    feeds = await _feeds(session)
    assert len(feeds) == 1 and feeds[0].id == rss_id


async def test_reconcile_leaves_non_owned_sub_settings_untouched(session):
    """A subscription at the same (platform, channel, feed) that isn't owned by
    sources.yaml must not have its settings rewritten by the file."""
    # Build a non-RSS source feed + a foreign (non-source-yaml) sub on it.
    await _reconcile(session, [_src(subs=[])])
    await session.commit()
    feed = (await _feeds(session))[0]

    foreign = Subscription(
        platform="discord",
        platform_user_id="a-real-human",  # not "source-yaml"
        platform_channel_id="123",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        target_language="ja",
        silent=True,
    )
    session.add(foreign)
    await session.commit()

    # The file now declares a discord/123 subscriber with different settings.
    await _reconcile(
        session,
        [_src(subs=[SubscriberCfg(
            platform="discord", channel="123", translate=True, language="en"
        )])],
    )
    await session.commit()

    subs = await _subs(session)
    assert len(subs) == 1  # the unique index prevented a duplicate
    assert subs[0].platform_user_id == "a-real-human"
    # Untouched: still the human's settings, not the file's.
    assert subs[0].translate is False
    assert subs[0].target_language == "ja"
    assert subs[0].silent is True
