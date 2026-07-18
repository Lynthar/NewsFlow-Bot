"""Ownership boundary between webhook_sync and source_sync.

Both syncs run back-to-back on every startup (main.py: webhooks first, then
sources) and both reconcile by deleting rows that aren't in *their* file.
sources.yaml may declare webhook-platform subscribers, so both syncs create
platform="webhook" Subscription rows — only the platform_user_id marker
("yaml" vs "source-yaml") keeps them from clobbering each other. Regression
tests for the restart-loses-backlog bug where webhook_sync deleted
source_sync's subscriptions (and their SentEntry history) on every boot.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.models.webhook import WebhookDestination
from newsflow.services.source_sync import SourceCfg, SubscriberCfg, _reconcile
from newsflow.services.webhook_sync import sync_webhooks

WEBHOOKS_YAML = """
destinations:
  slack:
    url: https://hooks.slack.com/x
    format: slack
subscriptions:
  slack:
    - https://feed.example.com/rss
"""


def _write_webhooks(tmp_path: Path, content: str = WEBHOOKS_YAML) -> Path:
    p = tmp_path / "webhooks.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _source_cfg(channel: str = "slack") -> SourceCfg:
    """A sources.yaml source whose subscriber delivers to a webhook
    destination — the exact layout samples/sources.example.yaml recommends."""
    return SourceCfg(
        name="api1",
        url="https://api.example.com/items",
        type="json_api",
        config={"items": "$.data[*]", "guid": "id"},
        subscribers=[SubscriberCfg(platform="webhook", channel=channel)],
    )


def _patch_session_factory(monkeypatch, session):
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "newsflow.services.webhook_sync.get_session_factory",
        lambda: lambda: _Ctx(),
    )


def _patch_feed_fetcher(monkeypatch):
    mock_fetcher = AsyncMock()
    mock_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="",
            success=True,
            entries=[
                {
                    "guid": "e1",
                    "title": "First entry",
                    "link": "https://feed.example.com/e1",
                }
            ],
            etag=None,
            last_modified=None,
            feed_title="Test Feed",
            feed_description=None,
            feed_link=None,
        )
    )
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: mock_fetcher,
    )


async def _source_yaml_subs(session) -> list[Subscription]:
    return (
        (
            await session.execute(
                select(Subscription).where(Subscription.platform_user_id == "source-yaml")
            )
        )
        .scalars()
        .all()
    )


async def _boot(session, tmp_path, sources) -> None:
    """One simulated startup: webhook_sync then source_sync, main.py order."""
    await sync_webhooks(_write_webhooks(tmp_path))
    await _reconcile(session, sources)
    await session.commit()


async def test_restart_preserves_source_yaml_subscription_and_history(
    session, monkeypatch, tmp_path
):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)
    sources = [_source_cfg()]

    await _boot(session, tmp_path, sources)

    subs = await _source_yaml_subs(session)
    assert len(subs) == 1
    original_id = subs[0].id

    # Simulate delivered history: a not-yet-cleaned dedupe record. The bug
    # cascaded this away with the subscription on every restart.
    session.add(SentEntry(subscription_id=original_id, feed_id=subs[0].feed_id, guid="seen-1"))
    await session.commit()

    await _boot(session, tmp_path, sources)  # second startup

    subs = await _source_yaml_subs(session)
    assert len(subs) == 1
    assert subs[0].id == original_id, "subscription must survive, not be recreated"
    sent = (
        (await session.execute(select(SentEntry).where(SentEntry.subscription_id == original_id)))
        .scalars()
        .all()
    )
    assert [s.guid for s in sent] == ["seen-1"]


async def test_destination_removal_spares_source_yaml_subscription(session, monkeypatch, tmp_path):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)
    sources = [_source_cfg(channel="slack")]

    await _boot(session, tmp_path, sources)
    assert len(await _source_yaml_subs(session)) == 1

    # Operator drops the destination from webhooks.yaml. webhook_sync must
    # delete the destination and ITS OWN subscription, but the source-yaml
    # row pointing at the same destination name stays source_sync's problem.
    await sync_webhooks(_write_webhooks(tmp_path, "destinations: {}\n"))
    await session.commit()

    dests = (await session.execute(select(WebhookDestination))).scalars().all()
    assert dests == []
    yaml_owned = (
        (await session.execute(select(Subscription).where(Subscription.platform_user_id == "yaml")))
        .scalars()
        .all()
    )
    assert yaml_owned == []
    assert len(await _source_yaml_subs(session)) == 1


async def test_webhook_sync_does_not_rewrite_source_yaml_settings(session, monkeypatch, tmp_path):
    """If webhooks.yaml lists a (destination, feed) pair that sources.yaml
    already owns, webhook_sync must neither rewrite its settings nor create a
    duplicate row (which would double-deliver every entry)."""
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)
    # Source subscriber with non-default settings, delivering to "slack".
    sources = [
        SourceCfg(
            name="api1",
            url="https://api.example.com/items",
            type="json_api",
            config={"items": "$.data[*]", "guid": "id"},
            subscribers=[
                SubscriberCfg(
                    platform="webhook",
                    channel="slack",
                    translate=False,
                    language="en",
                    silent=True,
                )
            ],
        )
    ]
    await _boot(session, tmp_path, sources)

    # webhooks.yaml now also claims the source feed's URL under "slack".
    # Destination defaults (translate=True, zh-CN) must NOT leak onto the
    # source-yaml row.
    conflicting = WEBHOOKS_YAML.replace(
        "- https://feed.example.com/rss",
        "- https://feed.example.com/rss\n    - https://api.example.com/items",
    )
    await sync_webhooks(_write_webhooks(tmp_path, conflicting))
    await session.commit()

    subs = await _source_yaml_subs(session)
    assert len(subs) == 1
    assert subs[0].translate is False
    assert subs[0].target_language == "en"
    assert subs[0].silent is True

    all_slack = (
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.platform == "webhook",
                    Subscription.platform_channel_id == "slack",
                )
            )
        )
        .scalars()
        .all()
    )
    # One row per feed: webhook_sync's own RSS sub + the source-yaml sub.
    assert len(all_slack) == 2
