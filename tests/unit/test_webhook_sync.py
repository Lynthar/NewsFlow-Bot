"""Tests for YAML parsing and DB reconciliation in webhook_sync."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from newsflow.core.feed_fetcher import FeedFetcher, FetchResult
from newsflow.models.feed import Feed
from newsflow.models.subscription import Subscription
from newsflow.models.webhook import WebhookDestination
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.feed_service import FeedService
from newsflow.services.webhook_sync import (
    WebhookConfigError,
    parse_webhooks_yaml,
    sync_webhooks,
)
from tests import seed


@pytest_asyncio.fixture
async def session(db):
    """A session on the shared test database; the code under test opens its own."""
    async with db() as s:
        yield s


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
    # Same default as a sources.yaml subscriber: translation is opt-in per destination.
    assert config.destinations["a"].translate is False
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


def test_parse_rejects_unknown_destination_key(tmp_path):
    # `secert:` (typo'd secret) used to be silently ignored — the HMAC
    # signature just vanished. Unknown keys are hard errors now.
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
    secert: oops
""",
    )
    with pytest.raises(WebhookConfigError, match="secert"):
        parse_webhooks_yaml(path)


def test_parse_rejects_unknown_top_level_key(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
subscriptons:
  a:
    - https://feed.example.com/rss
""",
    )
    with pytest.raises(WebhookConfigError, match="subscriptons"):
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


def test_malformed_yaml_error_does_not_quote_the_secret(tmp_path):
    # PyYAML quotes the source around the error when it parses a str; the error
    # here sits on the secret's own line.
    path = _write(tmp_path, 'destinations:\n  a:\n    url: https://e.com/a\n    secret: "s3cret\n')
    with pytest.raises(WebhookConfigError, match="malformed YAML") as exc:
        parse_webhooks_yaml(path)
    assert "s3cret" not in str(exc.value)
    assert "line 4" in str(exc.value)


@pytest.mark.parametrize("url", ["ftp://e.com/hook", "hooks.example.com/T0/TOKEN"])
def test_parse_rejects_a_url_that_is_not_http_and_does_not_echo_it(tmp_path, url):
    path = _write(tmp_path, f"destinations:\n  a:\n    url: {url}\n")
    with pytest.raises(WebhookConfigError, match="http") as exc:
        parse_webhooks_yaml(path)
    assert url not in str(exc.value)


def test_parse_keeps_env_references_unexpanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK_URL", "https://hooks.example.com/T0/TOKEN")
    monkeypatch.setenv("HOOK_SECRET", "s3cret")
    path = _write(
        tmp_path,
        "destinations:\n  a:\n    url: ${HOOK_URL}\n    secret: ${HOOK_SECRET}\n"
        "    headers:\n      Authorization: Bearer ${HOOK_SECRET}\n",
    )
    dest = parse_webhooks_yaml(path).destinations["a"]
    # What gets stored is the reference: the secrets never reach the database.
    assert (dest.url, dest.secret) == ("${HOOK_URL}", "${HOOK_SECRET}")
    assert dest.headers == {"Authorization": "Bearer ${HOOK_SECRET}"}


def test_parse_rejects_a_reference_to_an_unset_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("HOOK_SECRET", raising=False)
    path = _write(
        tmp_path, "destinations:\n  a:\n    url: https://e.com/h\n    secret: ${HOOK_SECRET}\n"
    )
    with pytest.raises(WebhookConfigError, match="'HOOK_SECRET', which is not set"):
        parse_webhooks_yaml(path)


def test_parse_checks_the_scheme_of_the_expanded_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK_URL", "ftp://e.com/hook")
    path = _write(tmp_path, "destinations:\n  a:\n    url: ${HOOK_URL}\n")
    with pytest.raises(WebhookConfigError, match="http"):
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


async def test_sync_creates_destination_and_subscription(session, monkeypatch, tmp_path):
    seed.patch_feed_fetcher(monkeypatch)

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
        (await session.execute(select(Subscription).where(Subscription.platform == "webhook")))
        .scalars()
        .all()
    )
    assert len(subs) == 1
    assert subs[0].platform_channel_id == "slack"
    assert subs[0].feed_id == feeds[0].id


async def test_sync_is_idempotent(session, monkeypatch, tmp_path):
    seed.patch_feed_fetcher(monkeypatch)

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
        (await session.execute(select(Subscription).where(Subscription.platform == "webhook")))
        .scalars()
        .all()
    )
    assert len(dests) == 1
    assert len(subs) == 1


async def test_sync_reenables_a_breaker_tripped_destination(session, monkeypatch, tmp_path):
    """A destination still declared in the file is one the operator wants
    working — sync (startup or hot reload) closes the circuit breaker."""
    seed.patch_feed_fetcher(monkeypatch)
    session.add(
        WebhookDestination(
            name="a",
            url="https://example.com/h",
            is_active=False,
            error_count=10,
            last_error="HTTP 500",
        )
    )
    await session.commit()
    path = _write(tmp_path, "destinations:\n  a:\n    url: https://example.com/h\n")

    await sync_webhooks(path)

    dest = (await session.execute(select(WebhookDestination))).scalars().one()
    assert dest.is_active is True
    assert dest.error_count == 0
    assert dest.last_error is None


async def test_sync_removes_destination_and_its_subscriptions(session, monkeypatch, tmp_path):
    seed.patch_feed_fetcher(monkeypatch)

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

    assert (await session.execute(select(WebhookDestination))).scalars().all() == []
    assert (
        await session.execute(select(Subscription).where(Subscription.platform == "webhook"))
    ).scalars().all() == []


async def test_sync_updates_destination_url(session, monkeypatch, tmp_path):
    seed.patch_feed_fetcher(monkeypatch)

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

    dest = (await session.execute(select(WebhookDestination))).scalars().one()
    assert dest.url == "https://new.example.com/a"
    assert dest.format == "slack"


async def test_sync_drops_subscription_when_feed_removed_from_yaml(session, monkeypatch, tmp_path):
    seed.patch_feed_fetcher(monkeypatch)

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
            (await session.execute(select(Subscription).where(Subscription.platform == "webhook")))
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
        (await session.execute(select(Subscription).where(Subscription.platform == "webhook")))
        .scalars()
        .all()
    )
    assert len(remaining) == 1


async def test_feed_whose_first_fetch_fails_is_subscribed_and_seeded_later(
    session, monkeypatch, tmp_path
):
    """The source is down when the file is synced. The subscription is created anyway
    and the dispatch loop retries the feed; when it first answers, only what was
    published before the subscription counts as backlog. Skipping the feed used to
    leave no subscription until the next restart, which then seeded everything
    published meanwhile."""
    fetcher = AsyncMock()
    fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(url="", success=False, entries=[], error="HTTP 503")
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)
    path = _write(
        tmp_path,
        "destinations:\n  a:\n    url: https://example.com/a\n"
        "subscriptions:\n  a:\n    - https://down.example.com/rss\n",
    )
    synced_at = datetime.now(UTC)
    await sync_webhooks(path)

    [sub] = (await session.execute(select(Subscription))).scalars().all()
    feed = await session.get(Feed, sub.feed_id)
    assert feed is not None and feed.last_successful_fetch_at is None

    def item(guid: str, published: datetime | None) -> dict:
        return {"guid": guid, "title": guid, "link": f"https://x/{guid}", "published_at": published}

    fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url=feed.url,
            success=True,
            entries=[
                item("before", synced_at - timedelta(days=1)),
                item("undated", None),
                item("after", synced_at + timedelta(minutes=5)),
            ],
        )
    )
    await FeedService(session).fetch_and_store(feed)
    await session.commit()

    unsent = await SubscriptionRepository(session).get_unsent_entries_for_subscription(sub.id)
    assert [e.guid for e in unsent] == ["after"]


async def test_sync_skips_a_feed_url_it_may_not_fetch(session, monkeypatch, tmp_path):
    """A URL the fetcher refuses outright is a configuration error, not an outage:
    nothing would ever succeed, so nothing is created."""
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: FeedFetcher())
    path = _write(
        tmp_path,
        "destinations:\n  a:\n    url: https://example.com/a\n"
        "subscriptions:\n  a:\n    - http://10.0.0.5/rss\n",
    )
    await sync_webhooks(path)

    assert (await session.execute(select(Feed))).scalars().all() == []
    assert (await session.execute(select(Subscription))).scalars().all() == []


async def test_sync_reactivates_auto_disabled_feed(session, monkeypatch, tmp_path):
    """A feed still declared in webhooks.yaml is revived on restart after an
    auto-disable — the deactivation notice promises exactly this for
    YAML-declared feeds."""
    seed.patch_feed_fetcher(monkeypatch)
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

    feed = (await session.execute(select(Feed))).scalars().one()
    feed.is_active = False
    feed.error_count = 10
    await session.commit()

    await sync_webhooks(path)

    await session.refresh(feed)
    assert feed.is_active is True
    assert feed.error_count == 0


def test_quoted_false_bool_is_rejected_not_coerced(tmp_path):
    """`translate: "false"` is a non-empty string — bool() coercion turned
    it into True, silently inverting the operator's intent. Strict check:
    non-bool → config error at parse time."""
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
    translate: "false"
""",
    )
    with pytest.raises(WebhookConfigError, match="translate"):
        parse_webhooks_yaml(path)


def test_unquoted_yaml_booleans_still_work(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
    translate: off
""",
    )
    assert parse_webhooks_yaml(path).destinations["a"].translate is False


def test_non_string_secret_is_rejected(tmp_path):
    """int-coercion would lose leading zeros and silently change the HMAC
    key; require an explicit string."""
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
    secret: 12345
""",
    )
    with pytest.raises(WebhookConfigError, match="secret"):
        parse_webhooks_yaml(path)


def test_non_string_header_names_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
destinations:
  a:
    url: https://example.com/hook
    headers:
      1: value
""",
    )
    with pytest.raises(WebhookConfigError, match="header names"):
        parse_webhooks_yaml(path)


async def test_db_error_text_does_not_carry_bound_secrets(session):
    # Any write failure (a lock timeout, a full disk) reaches the logs as the
    # exception's text; a unique clash is the easy one to provoke.
    def dest() -> WebhookDestination:
        return WebhookDestination(name="a", url="https://e.com/TOKEN1", secret="s3cret")

    session.add(dest())
    await session.commit()
    session.add(dest())
    with pytest.raises(IntegrityError) as exc:
        await session.commit()

    assert "s3cret" not in str(exc.value)
    assert "TOKEN1" not in str(exc.value)
