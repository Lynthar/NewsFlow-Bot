"""Tests for YAML parsing and DB reconciliation in webhook_sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.feed import Feed
from newsflow.models.subscription import Subscription
from newsflow.models.webhook import WebhookDestination
from newsflow.services.webhook_sync import (
    WebhookConfigError,
    parse_webhooks_yaml,
    sync_webhooks,
)


# ─── parse_webhooks_yaml ─────────────────────────────────────────────────────


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "webhooks.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_parse_minimal_valid_yaml(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
    format: generic
subscriptions:
  a:
    - https://feed.example.com/rss
""",
    )
    config = parse_webhooks_yaml(path)
    assert list(config.destinations) == ["a"]
    assert config.destinations["a"].url == "https://example.com/hook"
    assert config.destinations["a"].format == "generic"
    assert config.subscriptions == {"a": ["https://feed.example.com/rss"]}


def test_parse_carries_destination_defaults(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  slack:
    url: https://hooks.slack.com/x
    format: slack
    secret: s3cret
    headers:
      Authorization: Bearer xyz
    timeout_s: 5
    translate: false
    language: en
""",
    )
    config = parse_webhooks_yaml(path)
    d = config.destinations["slack"]
    assert d.secret == "s3cret"
    assert d.headers == {"Authorization": "Bearer xyz"}
    assert d.timeout_s == 5
    assert d.translate is False
    assert d.language == "en"


def test_parse_rejects_unknown_format(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  x:
    url: https://example.com
    format: telepathy
""",
    )
    with pytest.raises(WebhookConfigError, match="unsupported format"):
        parse_webhooks_yaml(path)


def test_parse_rejects_missing_url(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  x:
    format: generic
""",
    )
    with pytest.raises(WebhookConfigError, match="missing or non-string `url`"):
        parse_webhooks_yaml(path)


def test_parse_rejects_subscription_to_unknown_destination(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  b:
    - https://feed.example.com/rss
""",
    )
    with pytest.raises(WebhookConfigError, match="unknown destination"):
        parse_webhooks_yaml(path)


def test_parse_dedupes_feed_urls(tmp_path):
    """Duplicate URLs in a subscription list collapse to one subscription."""
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  a:
    - https://f.example.com/rss
    - https://f.example.com/rss
""",
    )
    config = parse_webhooks_yaml(path)
    assert config.subscriptions == {"a": ["https://f.example.com/rss"]}


def test_parse_rejects_non_mapping_root(tmp_path):
    path = _write(tmp_path, "- just a list")
    with pytest.raises(WebhookConfigError, match="top-level must be a mapping"):
        parse_webhooks_yaml(path)


def test_parse_rejects_malformed_yaml(tmp_path):
    path = _write(tmp_path, "destinations:\n  a: [unterminated")
    with pytest.raises(WebhookConfigError, match="malformed YAML"):
        parse_webhooks_yaml(path)


def test_parse_allows_empty_subscriptions(tmp_path):
    """Destination with no subs is valid — user might be staging one in."""
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com
""",
    )
    config = parse_webhooks_yaml(path)
    assert config.destinations
    assert config.subscriptions == {}


# ─── sync_webhooks (DB reconciliation) ───────────────────────────────────────


def _patch_session_factory(monkeypatch, session):
    """Make sync_webhooks use the test fixture session instead of opening
    a fresh one. Also patches the factory that FeedService walks through."""

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    factory = lambda: _Ctx()  # noqa: E731
    monkeypatch.setattr(
        "newsflow.services.webhook_sync.get_session_factory",
        lambda: factory,
    )


def _patch_feed_fetcher(monkeypatch, entries: list[dict] | None = None):
    """Stub the fetcher so add_feed succeeds without network I/O."""
    mock_fetcher = AsyncMock()
    mock_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="",
            success=True,
            entries=entries or [
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


async def test_sync_creates_destination_and_subscription(
    session, monkeypatch, tmp_path
):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)

    path = _write(
        tmp_path,
        """
destinations:
  slack:
    url: https://hooks.slack.com/x
    format: slack
subscriptions:
  slack:
    - https://feed.example.com/rss
""",
    )

    await sync_webhooks(path)

    dests = (await session.execute(select(WebhookDestination))).scalars().all()
    assert [d.name for d in dests] == ["slack"]
    assert dests[0].format == "slack"

    feeds = (await session.execute(select(Feed))).scalars().all()
    assert [f.url for f in feeds] == ["https://feed.example.com/rss"]

    subs = (
        await session.execute(
            select(Subscription).where(Subscription.platform == "webhook")
        )
    ).scalars().all()
    assert len(subs) == 1
    assert subs[0].platform_channel_id == "slack"
    assert subs[0].feed_id == feeds[0].id


async def test_sync_is_idempotent(session, monkeypatch, tmp_path):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)

    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  a:
    - https://feed.example.com/rss
""",
    )
    await sync_webhooks(path)
    await sync_webhooks(path)  # second run should be a no-op

    dests = (await session.execute(select(WebhookDestination))).scalars().all()
    subs = (
        await session.execute(
            select(Subscription).where(Subscription.platform == "webhook")
        )
    ).scalars().all()
    assert len(dests) == 1
    assert len(subs) == 1


async def test_sync_removes_destination_and_its_subscriptions(
    session, monkeypatch, tmp_path
):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)

    # Initial state: one destination + sub
    initial = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  a:
    - https://feed.example.com/rss
""",
    )
    await sync_webhooks(initial)
    assert (await session.execute(select(Subscription))).scalars().all()

    # Remove everything
    empty = _write(
        tmp_path,
        """
destinations: {}
subscriptions: {}
""",
    )
    await sync_webhooks(empty)

    assert (
        (await session.execute(select(WebhookDestination))).scalars().all() == []
    )
    assert (
        (
            await session.execute(
                select(Subscription).where(Subscription.platform == "webhook")
            )
        )
        .scalars()
        .all()
        == []
    )


async def test_sync_updates_destination_url(session, monkeypatch, tmp_path):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)

    p1 = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://old.example.com/a
""",
    )
    await sync_webhooks(p1)

    p2 = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://new.example.com/a
    format: slack
""",
    )
    await sync_webhooks(p2)

    dest = (
        (await session.execute(select(WebhookDestination))).scalars().one()
    )
    assert dest.url == "https://new.example.com/a"
    assert dest.format == "slack"


async def test_sync_drops_subscription_when_feed_removed_from_yaml(
    session, monkeypatch, tmp_path
):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)

    p1 = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  a:
    - https://f1.example.com/rss
    - https://f2.example.com/rss
""",
    )
    await sync_webhooks(p1)
    assert (
        len(
            (
                await session.execute(
                    select(Subscription).where(Subscription.platform == "webhook")
                )
            )
            .scalars()
            .all()
        )
        == 2
    )

    # Remove f2
    p2 = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  a:
    - https://f1.example.com/rss
""",
    )
    await sync_webhooks(p2)

    remaining = (
        (
            await session.execute(
                select(Subscription).where(Subscription.platform == "webhook")
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 1


async def test_sync_skips_feed_that_fails_to_add(
    session, monkeypatch, tmp_path
):
    """A feed URL that 404s shouldn't fail the whole sync — just skip it."""
    _patch_session_factory(monkeypatch, session)

    failing_fetcher = AsyncMock()
    failing_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="",
            success=False,
            entries=[],
            etag=None,
            last_modified=None,
            error="HTTP 404",
        )
    )
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: failing_fetcher,
    )

    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/a
subscriptions:
  a:
    - https://dead.example.com/rss
""",
    )
    await sync_webhooks(path)

    # Destination gets created, subscription does not.
    assert (
        len((await session.execute(select(WebhookDestination))).scalars().all())
        == 1
    )
    assert (
        len(
            (
                await session.execute(
                    select(Subscription).where(Subscription.platform == "webhook")
                )
            )
            .scalars()
            .all()
        )
        == 0
    )
