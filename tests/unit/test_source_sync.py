"""Tests for sources.yaml parsing + reconcile.

Covers: parse validation; reconcile create / idempotency / update / removal of
sources and individual subscribers; and the safety guarantee that RSS feeds and
non-owned subscriptions are never touched.
"""

import pytest
from sqlalchemy import select

from newsflow.models.feed import Feed
from newsflow.models.subscription import Subscription
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
