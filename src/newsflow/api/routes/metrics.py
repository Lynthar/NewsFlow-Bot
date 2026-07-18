"""Prometheus metrics endpoint.

Hand-rendered text exposition on purpose: a dozen unlabeled counters and
gauges are two stable lines each, which doesn't justify a prometheus-client
dependency in the ``api`` extra. Counters come from the dispatcher's
process-lifetime totals; gauges are cheap COUNT queries evaluated per scrape.

Behind the same read gate as the other data-bearing endpoints — Prometheus
scrape configs pass the key via ``authorization: {credentials: <API_KEY>}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db
from newsflow.models.digest import ChannelDigest
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import Subscription
from newsflow.models.webhook import WebhookDestination
from newsflow.services import get_dispatcher

router = APIRouter()

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _metric(name: str, kind: str, help_text: str, value: int) -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} {kind}\n{name} {value}\n"


async def _count(db: AsyncSession, stmt: Select[tuple[int]]) -> int:
    return int(await db.scalar(stmt) or 0)


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    totals = get_dispatcher().totals

    feeds_total = await _count(db, select(func.count()).select_from(Feed))
    feeds_active = await _count(
        db, select(func.count()).select_from(Feed).where(Feed.is_active.is_(True))
    )
    subs_total = await _count(db, select(func.count()).select_from(Subscription))
    subs_active = await _count(
        db,
        select(func.count()).select_from(Subscription).where(Subscription.is_active.is_(True)),
    )
    entries_total = await _count(db, select(func.count()).select_from(FeedEntry))
    digests_enabled = await _count(
        db,
        select(func.count()).select_from(ChannelDigest).where(ChannelDigest.enabled.is_(True)),
    )
    dests_total = await _count(db, select(func.count()).select_from(WebhookDestination))
    dests_active = await _count(
        db,
        select(func.count())
        .select_from(WebhookDestination)
        .where(WebhookDestination.is_active.is_(True)),
    )

    body = "".join(
        [
            _metric(
                "newsflow_dispatch_rounds_total",
                "counter",
                "Dispatch rounds completed since process start",
                totals.dispatch_rounds,
            ),
            _metric(
                "newsflow_feeds_fetched_total",
                "counter",
                "Feed fetch attempts since process start",
                totals.feeds_fetched,
            ),
            _metric(
                "newsflow_entries_ingested_total",
                "counter",
                "New entries stored since process start",
                totals.new_entries,
            ),
            _metric(
                "newsflow_messages_sent_total",
                "counter",
                "Messages delivered since process start",
                totals.messages_sent,
            ),
            _metric(
                "newsflow_send_errors_total",
                "counter",
                "Send/dispatch errors since process start",
                totals.send_errors,
            ),
            _metric("newsflow_feeds", "gauge", "Feeds in the database", feeds_total),
            _metric("newsflow_feeds_active", "gauge", "Active (fetchable) feeds", feeds_active),
            _metric("newsflow_subscriptions", "gauge", "Subscriptions", subs_total),
            _metric("newsflow_subscriptions_active", "gauge", "Active subscriptions", subs_active),
            _metric("newsflow_feed_entries", "gauge", "Stored feed entries", entries_total),
            _metric(
                "newsflow_digests_enabled", "gauge", "Channels with digest on", digests_enabled
            ),
            _metric("newsflow_webhook_destinations", "gauge", "Webhook destinations", dests_total),
            _metric(
                "newsflow_webhook_destinations_active",
                "gauge",
                "Webhook destinations with a closed circuit breaker",
                dests_active,
            ),
        ]
    )
    return PlainTextResponse(content=body, media_type=_CONTENT_TYPE)
