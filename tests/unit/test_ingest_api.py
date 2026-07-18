"""Tests for the inbound ingest API + API-key auth.

The route functions are called directly (FastAPI's Depends defaults are
overridden with explicit args), so these stay offline and pin the auth + write
+ dedupe logic without spinning up a server.
"""

import pytest

pytest.importorskip("fastapi")  # needs the api extra

from fastapi import HTTPException  # noqa: E402

from newsflow.api.deps import require_api_key  # noqa: E402
from newsflow.api.routes.ingest import (  # noqa: E402
    IngestEntry,
    IngestPayload,
    _to_entry_dict,
    ingest,
)
from newsflow.models.feed import Feed  # noqa: E402


class _FakeSettings:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key


# ── auth ─────────────────────────────────────────────────────────────────────


async def test_auth_fails_closed_when_no_key(monkeypatch):
    monkeypatch.setattr("newsflow.api.deps.get_settings", lambda: _FakeSettings(""))
    with pytest.raises(HTTPException) as exc:
        await require_api_key(authorization="Bearer anything")
    assert exc.value.status_code == 503  # no key configured → writes disabled


async def test_auth_rejects_wrong_and_missing(monkeypatch):
    monkeypatch.setattr("newsflow.api.deps.get_settings", lambda: _FakeSettings("secret"))
    for header in ("Bearer wrong", None, "", "Bearer "):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(authorization=header)
        assert exc.value.status_code == 401


async def test_auth_accepts_bearer_and_raw(monkeypatch):
    monkeypatch.setattr("newsflow.api.deps.get_settings", lambda: _FakeSettings("secret"))
    # Neither call should raise.
    await require_api_key(authorization="Bearer secret")
    await require_api_key(authorization="secret")


# ── mapping ──────────────────────────────────────────────────────────────────


def test_to_entry_dict_maps_fields_and_hashes_guid():
    d = _to_entry_dict(IngestEntry(id="a1", title="T", url="https://x/a", summary="s"), "slug")
    assert d["guid"] == "a1" and d["title"] == "T" and d["link"] == "https://x/a"

    # No id → distinct content hashes; link falls back to the feed slug.
    d2 = _to_entry_dict(IngestEntry(title="one"), "slug")
    d3 = _to_entry_dict(IngestEntry(title="two"), "slug")
    assert d2["guid"] != d3["guid"]
    assert d2["link"] == "slug"


# ── ingest route ─────────────────────────────────────────────────────────────


async def test_ingest_writes_and_is_idempotent(session):
    session.add(Feed(url="my-inbound", source_type="webhook_inbound"))
    await session.commit()

    payload = IngestPayload(entries=[IngestEntry(id="e1", title="Hello", url="https://x/e1")])
    res = await ingest(source="my-inbound", payload=payload, db=session, _=None)
    await session.commit()
    assert res.accepted == 1 and res.created == 1

    # Re-POST the same id → deduped, nothing new created.
    res2 = await ingest(source="my-inbound", payload=payload, db=session, _=None)
    await session.commit()
    assert res2.created == 0


async def test_ingest_unknown_source_404(session):
    with pytest.raises(HTTPException) as exc:
        await ingest(source="nope", payload=IngestPayload(entries=[]), db=session, _=None)
    assert exc.value.status_code == 404


async def test_ingest_rejects_non_inbound_feed(session):
    # An RSS feed is not a push source — must 404, not accept writes.
    session.add(Feed(url="https://rss.example.com/feed", source_type="rss"))
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await ingest(
            source="https://rss.example.com/feed",
            payload=IngestPayload(entries=[]),
            db=session,
            _=None,
        )
    assert exc.value.status_code == 404
