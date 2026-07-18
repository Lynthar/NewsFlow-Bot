"""Tests for the JSON-API source: JSONPath mapping, guid hash-fallback, SSRF
guard, config validation, bad-JSON handling, and lazy registration.

HTTP is bypassed (``_safe_get`` is stubbed) so these stay offline and pin the
mapping/guard logic, not aiohttp.
"""

import json
from datetime import UTC

import pytest

from newsflow.core.source_fetcher import SourceRequest, get_source_fetcher
from newsflow.core.sources.json_api import JsonApiSourceFetcher

pytest.importorskip("jsonpath_ng")  # needs the source-json extra


def _fetcher_returning(payload: dict) -> JsonApiSourceFetcher:
    f = JsonApiSourceFetcher()
    raw = json.dumps(payload).encode()
    f.seen_headers: list[dict | None] = []  # type: ignore[attr-defined]

    async def fake_get(url: str, extra_headers: dict | None = None) -> bytes:
        f.seen_headers.append(extra_headers)  # type: ignore[attr-defined]
        return raw

    f._safe_get = fake_get  # type: ignore[method-assign]
    return f


async def test_maps_items_via_jsonpath():
    f = _fetcher_returning(
        {
            "data": [
                {
                    "id": "a",
                    "title": "A",
                    "url": "https://e/a",
                    "desc": "body",
                    "when": "2026-05-31T08:00:00Z",
                    "by": {"name": "Jo"},
                }
            ]
        }
    )
    req = SourceRequest(
        url="https://api.example.com/items",
        config={
            "items": "$.data[*]",
            "guid": "id",
            "title": "title",
            "link": "url",
            "summary": "desc",
            "published": "when",
            "author": "by.name",
        },
    )
    res = await f.fetch(req)

    assert res.success
    assert len(res.entries) == 1
    e = res.entries[0]
    assert e["guid"] == "a"
    assert e["title"] == "A"
    assert e["link"] == "https://e/a"
    assert e["summary"] == "body"
    assert e["author"] == "Jo"  # nested path resolved
    assert e["published_at"].tzinfo is UTC


async def test_guid_falls_back_to_distinct_hashes():
    f = _fetcher_returning({"items": [{"t": "one"}, {"t": "two"}]})
    req = SourceRequest(
        url="https://api.example.com/x",
        config={"items": "$.items[*]", "title": "t"},
    )
    res = await f.fetch(req)

    assert res.success
    guids = [e["guid"] for e in res.entries]
    assert len(guids) == 2 and guids[0] != guids[1]  # no collision
    assert all(e["link"] == "https://api.example.com/x" for e in res.entries)


async def test_missing_items_config_fails():
    res = await JsonApiSourceFetcher().fetch(
        SourceRequest(url="https://api.example.com/x", config={})
    )
    assert res.success is False
    assert "items" in (res.error or "")


async def test_ssrf_private_url_rejected():
    # validate_feed_url runs before any fetch, so a link-local target is
    # rejected outright. _safe_get is stubbed but never reached here.
    f = _fetcher_returning({"data": []})
    res = await f.fetch(
        SourceRequest(url="http://169.254.169.254/meta", config={"items": "$.data[*]"})
    )
    assert res.success is False
    assert "link-local" in (res.error or "") or "private" in (res.error or "")


async def test_invalid_json_response_fails():
    f = JsonApiSourceFetcher()

    async def bad_get(url: str, extra_headers: dict | None = None) -> bytes:
        return b"<html>not json</html>"

    f._safe_get = bad_get  # type: ignore[method-assign]
    res = await f.fetch(
        SourceRequest(url="https://api.example.com/x", config={"items": "$.data[*]"})
    )
    assert res.success is False
    assert "JSON" in (res.error or "")


def test_json_api_registered_lazily():
    # First request for 'json_api' triggers the lazy import + self-registration.
    fetcher = get_source_fetcher("json_api")
    assert fetcher is not None
    assert hasattr(fetcher, "fetch")


# ─── custom headers + ${ENV} interpolation ───────────────────────────────────


async def test_headers_reach_the_request_with_env_interpolation(monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "s3cret")
    f = _fetcher_returning({"data": [{"id": "a", "title": "A"}]})

    res = await f.fetch(
        SourceRequest(
            url="https://api.example.com/x",
            config={
                "items": "$.data[*]",
                "headers": {"Authorization": "Bearer ${MY_API_TOKEN}", "X-Fixed": "1"},
            },
        )
    )

    assert res.success is True
    assert f.seen_headers == [{"Authorization": "Bearer s3cret", "X-Fixed": "1"}]


async def test_missing_env_var_fails_without_leaking_values(monkeypatch):
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    f = _fetcher_returning({"data": []})

    res = await f.fetch(
        SourceRequest(
            url="https://api.example.com/x",
            config={"items": "$.data[*]", "headers": {"Authorization": "Bearer ${NOPE_TOKEN}"}},
        )
    )

    assert res.success is False
    assert "NOPE_TOKEN" in (res.error or "")
    assert "Authorization" in (res.error or "")
    assert f.seen_headers == []  # never reached the network


async def test_non_mapping_headers_config_fails():
    f = _fetcher_returning({"data": []})
    res = await f.fetch(
        SourceRequest(
            url="https://api.example.com/x",
            config={"items": "$.data[*]", "headers": "Authorization: x"},
        )
    )
    assert res.success is False
    assert "mapping" in (res.error or "")
