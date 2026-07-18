"""JSON-API source: fetch a REST/JSON endpoint and map array items to entries
via JSONPath. Optional dependency: ``jsonpath-ng`` (extra ``source-json``).

``Feed.config`` shape (``Feed.url`` holds the endpoint URL):

    items:     JSONPath to the array of items, e.g. ``"$.data[*]"``  (required)
    guid:      field path within each item used as the dedupe key — falls back
               to a content hash when absent, so distinct items never collapse
               to one guid (which dedupe would treat as already-sent)
    title, link, summary, content, published, image, author:
               optional field paths within each item
    headers:   mapping of extra request headers. Values may embed
               ``${ENV_VAR}`` references, resolved from the process
               environment at request time — the secret itself stays out of
               sources.yaml, the DB, and the logs. A reference to an unset
               variable fails the fetch loudly (the API would 401 anyway).

Field paths are JSONPath relative to each item; a plain field name (``"id"``)
and a nested path (``"author.name"``) both work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp
from dateutil import parser as date_parser

from newsflow.core.feed_fetcher import (
    DEFAULT_HEADERS,
    MAX_FEED_SIZE_BYTES,
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    FetchResult,
)
from newsflow.core.source_fetcher import SourceRequest, register_source_fetcher
from newsflow.core.url_security import InvalidFeedURLError, validate_feed_url

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
_FIELDS = (
    "guid",
    "title",
    "link",
    "summary",
    "content",
    "published",
    "image",
    "author",
)


def _fail(url: str, error: str) -> FetchResult:
    return FetchResult(url=url, success=False, entries=[], error=error)


_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_headers(raw: Any) -> dict[str, str]:
    """Expand ``${ENV_VAR}`` references in configured header values.

    Raises ValueError with a message that names the HEADER and the variable,
    never the resolved value — header values are Bearer tokens more often
    than not.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("config.headers must be a mapping of header -> value")
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)

        def expand(match: re.Match[str], _header: str = name) -> str:
            var = match.group(1)
            resolved = os.environ.get(var)
            if resolved is None:
                raise ValueError(
                    f"headers[{_header!r}] references environment variable "
                    f"{var!r}, which is not set"
                )
            return resolved

        out[name] = _ENV_REF_RE.sub(expand, str(value))
    return out


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    # A date with no offset parses naive; treat it as UTC (don't let
    # .astimezone() assume the host's local tz).
    dt: datetime = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class JsonApiSourceFetcher:
    """Fetch a JSON endpoint and map its items to entries via JSONPath."""

    async def fetch(self, req: SourceRequest) -> FetchResult:
        config = req.config or {}
        items_path = config.get("items")
        if not isinstance(items_path, str) or not items_path:
            return _fail(req.url, "json_api: config.items (JSONPath) is required")

        try:
            from jsonpath_ng.ext import parse as jp_parse
        except ImportError:
            return _fail(
                req.url,
                "json_api source needs the 'source-json' extra (pip install jsonpath-ng)",
            )

        try:
            validate_feed_url(req.url)
        except InvalidFeedURLError as e:
            return _fail(req.url, str(e))

        try:
            items_expr = jp_parse(items_path)
            field_exprs = {
                k: jp_parse(config[k])
                for k in _FIELDS
                if isinstance(config.get(k), str) and config[k]
            }
        except Exception as e:  # jsonpath parse errors aren't a single type
            return _fail(req.url, f"json_api: invalid JSONPath: {e}")

        try:
            extra_headers = _resolve_headers(config.get("headers"))
        except ValueError as e:
            return _fail(req.url, f"json_api: {e}")

        try:
            raw = await self._safe_get(req.url, extra_headers)
        except InvalidFeedURLError as e:
            return _fail(req.url, f"Unsafe redirect target: {e}")
        except Exception as e:
            return _fail(req.url, f"{type(e).__name__}: {e}")

        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            return _fail(req.url, f"json_api: response is not valid JSON: {e}")

        entries = [
            self._map_item(match.value, field_exprs, req.url)
            for match in items_expr.find(data)
            if isinstance(match.value, dict)
        ]
        return FetchResult(url=req.url, success=True, entries=entries)

    async def _safe_get(self, url: str, extra_headers: dict[str, str] | None = None) -> bytes:
        """GET with the same SSRF (per-hop revalidation) and size guards as the
        RSS fetcher. Raises on unsafe redirect, HTTP >= 400, or oversize body."""
        headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers) as session:
            current = url
            for _hop in range(MAX_REDIRECTS + 1):
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in REDIRECT_STATUSES:
                        location = resp.headers.get("Location")
                        if not location:
                            raise ValueError(f"HTTP {resp.status} redirect without Location")
                        current = urljoin(current, location)
                        validate_feed_url(current)  # raises on unsafe target
                        continue
                    if resp.status >= 400:
                        raise ValueError(f"HTTP {resp.status}")
                    raw = await resp.content.read(MAX_FEED_SIZE_BYTES + 1)
                    if len(raw) > MAX_FEED_SIZE_BYTES:
                        raise ValueError("response exceeds size limit")
                    return raw
            raise ValueError(f"too many redirects (>{MAX_REDIRECTS})")

    def _map_item(
        self, item: dict[str, Any], field_exprs: dict[str, Any], feed_url: str
    ) -> dict[str, Any]:
        def first(key: str) -> Any:
            expr = field_exprs.get(key)
            if expr is None:
                return None
            matches = expr.find(item)
            return matches[0].value if matches else None

        guid = first("guid")
        if guid is None or guid == "":
            # No id (or empty) → stable content hash so distinct items don't
            # collapse to one guid, which dedupe would treat as already-sent.
            basis = json.dumps(item, sort_keys=True, default=str)
            guid = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        image = first("image")
        return {
            "guid": str(guid),
            "title": _to_text(first("title")) or "Untitled",
            "link": _to_text(first("link")) or feed_url,
            "summary": _to_text(first("summary")) or "",
            "content": _to_text(first("content")),
            "author": _to_text(first("author")),
            "published_at": _parse_date(first("published")),
            "image_url": image if isinstance(image, str) else None,
        }


register_source_fetcher("json_api", JsonApiSourceFetcher())
