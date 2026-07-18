"""Subscription management endpoints.

The remote-control counterpart of the bot commands: the API could already
create feeds but had no way to make one deliver anywhere, so "manage the bot
over HTTP" was only half true. The management unit is a channel — list and
OPML export take (platform, channel) explicitly, mirroring the bot's own
scoping.

Reads are open like the other GET endpoints; every mutation requires the API
key. Pause / resume / delete address a subscription by id (from the list
response) so callers never re-type feed URLs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db, require_api_key
from newsflow.models.subscription import Subscription
from newsflow.services.subscription_service import SubscriptionService

router = APIRouter()


class SubscriptionResponse(BaseModel):
    id: int
    platform: str
    channel_id: str
    feed_url: str
    feed_title: str | None
    active: bool
    silent: bool
    translate: bool
    target_language: str
    show_summary: bool
    show_image: bool


class SubscriptionListResponse(BaseModel):
    subscriptions: list[SubscriptionResponse]
    total: int


class SubscribeRequest(BaseModel):
    platform: str
    channel_id: str
    feed_url: str
    guild_id: str | None = None


class ActionResponse(BaseModel):
    success: bool
    message: str


def _to_response(sub: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=sub.id,
        platform=sub.platform,
        channel_id=sub.platform_channel_id,
        feed_url=sub.feed.url,
        feed_title=sub.feed.title,
        active=sub.is_active,
        silent=sub.silent,
        translate=sub.translate,
        target_language=sub.target_language,
        show_summary=sub.show_summary,
        show_image=sub.show_image,
    )


@router.get("", response_model=SubscriptionListResponse)
async def list_subscriptions(
    platform: str,
    channel: str,
    db: AsyncSession = Depends(get_db),
) -> SubscriptionListResponse:
    """Every subscription (paused included) of one channel."""
    service = SubscriptionService(db)
    subs = await service.get_channel_subscriptions(
        platform=platform, channel_id=channel, include_inactive=True
    )
    return SubscriptionListResponse(
        subscriptions=[_to_response(s) for s in subs],
        total=len(subs),
    )


@router.get("/opml")
async def export_opml(
    platform: str,
    channel: str,
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """The channel's subscriptions as an OPML document (backup / migration)."""
    service = SubscriptionService(db)
    opml = await service.export_opml(platform=platform, channel_id=channel)
    return PlainTextResponse(content=opml, media_type="text/x-opml")


@router.post("", response_model=ActionResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    payload: SubscribeRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_api_key),
) -> ActionResponse:
    """Subscribe a channel to a feed. Missing feeds are added via the normal
    add path (fetch + validation), same as the bot's /add."""
    service = SubscriptionService(db)
    result = await service.subscribe(
        platform=payload.platform,
        user_id="api",
        channel_id=payload.channel_id,
        feed_url=payload.feed_url,
        guild_id=payload.guild_id,
    )
    if not result.success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.message)
    return ActionResponse(success=True, message=result.message)


async def _sub_or_404(service: SubscriptionService, sub_id: int) -> Subscription:
    sub = await service.get_subscription_by_id(sub_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No subscription with id {sub_id}",
        )
    return sub


@router.post("/{sub_id}/pause", response_model=ActionResponse)
async def pause_subscription(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_api_key),
) -> ActionResponse:
    service = SubscriptionService(db)
    sub = await _sub_or_404(service, sub_id)
    result = await service.pause_subscription(sub.platform, sub.platform_channel_id, sub.feed.url)
    return ActionResponse(success=result.success, message=result.message)


@router.post("/{sub_id}/resume", response_model=ActionResponse)
async def resume_subscription(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_api_key),
) -> ActionResponse:
    service = SubscriptionService(db)
    sub = await _sub_or_404(service, sub_id)
    result = await service.resume_subscription(sub.platform, sub.platform_channel_id, sub.feed.url)
    return ActionResponse(success=result.success, message=result.message)


@router.delete("/{sub_id}", response_model=ActionResponse)
async def delete_subscription(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_api_key),
) -> ActionResponse:
    """Unsubscribe. Cascades the subscription's filter and dedupe history —
    re-subscribing later starts fresh (same semantics as the bot's /remove)."""
    service = SubscriptionService(db)
    sub = await _sub_or_404(service, sub_id)
    result = await service.unsubscribe(sub.platform, sub.platform_channel_id, sub.feed.url)
    if not result.success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.message)
    return ActionResponse(success=True, message=result.message)
