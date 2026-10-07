"""Tests for sources.yaml parsing + reconcile.

Covers: parse validation; reconcile create / idempotency / update / removal of
sources and individual subscribers; and the safety guarantee that RSS feeds and
non-owned subscriptions are never touched.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from newsflow.core import source_fetcher as sf
from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService
from newsflow.services.source_sync import (
    SourceCfg,
    SourceConfigError,
    SubscriberCfg,
    _reconcile,
    parse_sources_yaml,
)

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


def _with_subscriber(tmp_path, subscriber: str):
    return _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        f"    subscribers:\n      - platform: discord\n        {subscriber}\n",
    )


def test_parse_rejects_a_channel_wider_than_its_column(tmp_path):
    p = _with_subscriber(tmp_path, f"channel: '{'9' * 65}'")
    with pytest.raises(SourceConfigError, match="`channel` is longer than 64"):
        parse_sources_yaml(p)


def test_parse_normalizes_the_language_and_rejects_a_non_code(tmp_path):
    p = _with_subscriber(tmp_path, "channel: '1'\n        language: ZH-tw")
    assert parse_sources_yaml(p)[0].subscribers[0].language == "zh-TW"

    p = _with_subscriber(tmp_path, "channel: '1'\n        language: chinese")
    with pytest.raises(SourceConfigError, match="not a language code"):
        parse_sources_yaml(p)


def test_parse_rejects_unknown_source_key(tmp_path):
    # Typo'd keys used to vanish silently; now they abort startup.
    p = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n    confg:\n      items: $\n",
    )
    with pytest.raises(SourceConfigError, match="confg"):
        parse_sources_yaml(p)


def test_parse_rejects_unknown_subscriber_key(tmp_path):
    p = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        "    subscribers:\n      - platform: discord\n        channel: '1'\n"
        "        silnet: true\n",
    )
    with pytest.raises(SourceConfigError, match="silnet"):
        parse_sources_yaml(p)


def test_parse_config_subkeys_stay_free_form(tmp_path):
    # `config:` keys belong to the individual SourceFetcher contracts —
    # arbitrary keys there must NOT be rejected by the schema layer.
    p = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        "    config:\n      items: '$.a'\n      custom_anything: 1\n",
    )
    srcs = parse_sources_yaml(p)
    assert srcs[0].config["custom_anything"] == 1


def test_parse_fetch_interval_is_a_source_level_key(tmp_path):
    p = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        "    fetch_interval_minutes: 240\n    config:\n      items: '$.a'\n",
    )
    assert parse_sources_yaml(p)[0].fetch_interval_minutes == 240

    bad = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        "    fetch_interval_minutes: 0\n    config:\n      items: '$.a'\n",
    )
    with pytest.raises(SourceConfigError, match="fetch_interval_minutes"):
        parse_sources_yaml(bad)


def test_parse_rejects_fetch_interval_inside_config(tmp_path):
    # Reserved scheduling key — inside config: it would silently shadow the
    # schema-level one, so it's rejected with a pointer to the right place.
    p = _write(
        tmp_path,
        "sources:\n  x:\n    url: https://e/x\n    type: json_api\n"
        "    config:\n      items: '$.a'\n      fetch_interval_minutes: 60\n",
    )
    with pytest.raises(SourceConfigError, match="source-level"):
        parse_sources_yaml(p)


# ── reconcile ────────────────────────────────────────────────────────────────


def _src(url: str = "https://api.example.com/items", subs=None) -> SourceCfg:
    return SourceCfg(
        name="api1",
        url=url,
        type="json_api",
        config={"items": "$.data[*]", "guid": "id"},
        subscribers=(
            subs if subs is not None else [SubscriberCfg(platform="discord", channel="123")]
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
            SubscriberCfg(platform="discord", channel="123", translate=True, language="en")
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
    await _reconcile(session, [_src(subs=[SubscriberCfg(platform="discord", channel="123")])])
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
        [
            _src(
                subs=[
                    SubscriberCfg(platform="discord", channel="123", translate=True, language="en")
                ]
            )
        ],
    )
    await session.commit()

    subs = await _subs(session)
    assert len(subs) == 1  # the unique index prevented a duplicate
    assert subs[0].platform_user_id == "a-real-human"
    # Untouched: still the human's settings, not the file's.
    assert subs[0].translate is False
    assert subs[0].target_language == "ja"
    assert subs[0].silent is True


async def test_reconcile_stores_and_removes_fetch_interval(session):
    # Declared interval lands in Feed.config under the reserved key…
    src = _src()
    src.fetch_interval_minutes = 240
    await _reconcile(session, [src])
    await session.commit()
    feed = (await _feeds(session))[0]
    assert feed.config["fetch_interval_minutes"] == 240

    # …and disappears again when the operator removes it from the file
    # (stored config is rebuilt on every sync).
    await _reconcile(session, [_src()])
    await session.commit()
    feed = (await _feeds(session))[0]
    assert "fetch_interval_minutes" not in feed.config


def test_quoted_bool_strings_are_rejected(tmp_path):
    """silent: "no" / translate: "false" are non-empty strings; bool()
    coercion made both True — the exact opposite of what was written."""
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  s1:
    type: webhook_inbound
    url: inbound-1
    subscribers:
      - platform: telegram
        channel: "123"
        silent: "no"
""",
        encoding="utf-8",
    )
    with pytest.raises(SourceConfigError, match="silent"):
        parse_sources_yaml(path)


# ── what a declared source delivers first ────────────────────────────────────


class _Serving:
    """A json_api source serving a fixed list of entries."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries

    async def fetch(self, req):
        return FetchResult(url=req.url, success=True, entries=self.entries)


def _served(declared_at: datetime) -> list[dict]:
    def item(guid: str, published: datetime | None) -> dict:
        return {"guid": guid, "title": guid, "link": f"https://x/{guid}", "published_at": published}

    return [
        item("old", declared_at - timedelta(days=1)),
        item("undated", None),
        item("new", declared_at + timedelta(minutes=5)),
    ]


async def _first_poll(session, monkeypatch, entries: list[dict]) -> list[str]:
    """Fetch every source once; return what the subscription would receive."""
    monkeypatch.setitem(sf._REGISTRY, "json_api", _Serving(entries))
    await FeedService(session).fetch_all_feeds()
    await session.commit()
    [sub] = await _subs(session)
    unsent = await SubscriptionRepository(session).get_unsent_entries_for_subscription(sub.id)
    return [e.guid for e in unsent]


async def test_new_source_delivers_only_what_follows_its_declaration(session, monkeypatch):
    """A new source has no entries when it is declared, so nothing could be seeded
    then; its whole first poll used to go out as new articles."""
    declared_at = datetime.now(UTC)
    await _reconcile(session, [_src()])
    await session.commit()

    assert await _first_poll(session, monkeypatch, _served(declared_at)) == ["new"]


async def test_source_removed_then_restored_does_not_repush(session, monkeypatch):
    declared_at = datetime.now(UTC)
    await _reconcile(session, [_src()])
    await session.commit()
    await _first_poll(session, monkeypatch, _served(declared_at))

    await _reconcile(session, [])  # commented out: the feed and its history go
    await session.commit()
    await _reconcile(session, [_src()])  # and back
    await session.commit()

    entries = _served(declared_at)
    for e in entries:
        e["published_at"] = e["published_at"] and e["published_at"] - timedelta(hours=1)
    assert await _first_poll(session, monkeypatch, entries) == []
