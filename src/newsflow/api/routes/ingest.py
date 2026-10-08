"""Inbound ingest endpoint: external systems POST entries INTO NewsFlow.

The push counterpart to the outbound webhook adapter. A ``webhook_inbound``
source (declared in sources.yaml together with its subscribers) holds the
pushed entries; this endpoint writes them through the normal ingestion path
and immediately triggers a dispatch round — "pushed" content shouldn't sit
waiting for the next scheduled cycle (up to a full fetch interval).

Idempotent by guid (client ``id`` or a content hash), so re-POSTing the same
item is a no-op. Writes require INGEST_API_KEY or API_KEY (see ``require_ingest_key``).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db, require_ingest_key
from newsflow.models.feed import ENTRY_CONTENT_MAX_CHARS, ENTRY_SUMMARY_MAX_CHARS
from newsflow.services.feed_service import FeedService

router = APIRouter()


class IngestEntry(BaseModel):
    """One pushed item. All fields optional; ``id`` is the dedupe key when
    present, otherwise a content hash is used.

    Field caps mirror the FeedEntry column widths (plus generous text-field
    ceilings): the endpoint is authenticated, but a leaked key or a buggy
    client shouldn't be able to push megabyte strings into the DB and the
    downstream render path. Oversize input fails loudly as a 422."""

    id: str | None = Field(None, max_length=2048)
    title: str | None = Field(None, max_length=1024)
    link: str | None = Field(None, max_length=2048)
    url: str | None = Field(None, max_length=2048)
    summary: str | None = Field(None, max_length=ENTRY_SUMMARY_MAX_CHARS)
    content: str | None = Field(None, max_length=ENTRY_CONTENT_MAX_CHARS)
    author: str | None = Field(None, max_length=256)
    image: str | None = Field(None, max_length=2048)
    published_at: datetime | None = None


class IngestPayload(BaseModel):
    entries: list[IngestEntry] = Field(max_length=1000)


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
    _: None = Depends(require_ingest_key),
) -> IngestResponse:
    """Accept pushed entries for a ``webhook_inbound`` source, looked up by the
    ``{source}`` slug (= the feed's url). Entries are written deduped-by-guid;
    the dispatch loop delivers them to the source's subscribers."""
    service = FeedService(db)
    feed = await service.get_feed_by_url(source)
    if feed is None or feed.source_type != "webhook_inbound":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No inbound source named {source!r}",
        )
    entry_dicts = [_to_entry_dict(e, feed.url) for e in payload.entries]
    created = await service.add_entries(feed.id, entry_dicts)

    if created:
        # Commit before triggering so the spawned round sees the new rows. A full
        # dispatch_once is deliberate: it is mutex-serialised with the loop and
        # fetch_all_feeds only touches feeds actually due.
        await db.commit()
        from newsflow.services import get_dispatcher

        dispatcher = get_dispatcher()
        dispatcher.spawn(dispatcher.dispatch_once(), name=f"ingest-dispatch-{feed.id}")

    return IngestResponse(accepted=len(payload.entries), created=len(created))
