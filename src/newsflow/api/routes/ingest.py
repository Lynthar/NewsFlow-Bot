"""Inbound ingest endpoint: external systems POST entries INTO NewsFlow.

The push counterpart to the outbound webhook adapter. A ``webhook_inbound``
source (declared in sources.yaml together with its subscribers) holds the
pushed entries; this endpoint writes them through the normal ingestion path
and immediately triggers a dispatch round — "pushed" content shouldn't sit
waiting for the next scheduled cycle (up to a full fetch interval).

Idempotent by guid (client ``id`` or a content hash), so re-POSTing the same
item is a no-op. Writes require the API key (see ``require_api_key``).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db, require_api_key
from newsflow.repositories.feed_repository import FeedRepository

router = APIRouter()


class IngestEntry(BaseModel):
    """One pushed item. All fields optional; ``id`` is the dedupe key when
    present, otherwise a content hash is used."""

    id: str | None = None
    title: str | None = None
    link: str | None = None
    url: str | None = None
    summary: str | None = None
    content: str | None = None
    author: str | None = None
    image: str | None = None
    published_at: datetime | None = None


class IngestPayload(BaseModel):
    entries: list[IngestEntry]


class IngestResponse(BaseModel):
    accepted: int
    created: int


def _to_entry_dict(e: IngestEntry, feed_url: str) -> dict[str, Any]:
    """Map a pushed item to the normalized entry dict the repo expects."""
    link = e.link or e.url or feed_url
    guid = e.id
    if not guid:
        basis = f"{e.title or ''}{link}{e.summary or ''}{e.content or ''}"
        guid = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return {
        "guid": str(guid),
        "title": e.title or "Untitled",
        "link": link,
        "summary": e.summary or "",
        "content": e.content,
        "author": e.author,
        "published_at": e.published_at,
        "image_url": e.image,
    }


@router.post(
    "/{source}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest(
    source: str,
    payload: IngestPayload,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_api_key),
) -> IngestResponse:
    """Accept pushed entries for a ``webhook_inbound`` source, looked up by the
    ``{source}`` slug (= the feed's url). Entries are written deduped-by-guid;
    the dispatch loop delivers them to the source's subscribers."""
    repo = FeedRepository(db)
    feed = await repo.get_feed_by_url(source)
    if feed is None or feed.source_type != "webhook_inbound":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No inbound source named {source!r}",
        )
    entry_dicts = [_to_entry_dict(e, feed.url) for e in payload.entries]
    created = await repo.create_entries_bulk(feed.id, entry_dicts)

    if created:
        # Commit before triggering so the spawned round sees the new rows
        # (get_db's own commit only runs after this handler returns). A full
        # dispatch_once is deliberate — it's mutex-serialised with the loop,
        # and fetch_all_feeds only touches feeds actually due, so a triggered
        # round is delivery-only in practice.
        await db.commit()
        from newsflow.services import get_dispatcher

        dispatcher = get_dispatcher()
        dispatcher.spawn(dispatcher.dispatch_once(), name=f"ingest-dispatch-{feed.id}")

    return IngestResponse(accepted=len(payload.entries), created=len(created))
