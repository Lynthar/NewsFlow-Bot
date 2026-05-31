"""SourceFetcher abstraction: pluggable per-source-type fetchers.

A feed's ``source_type`` selects how its entries are fetched. ``rss`` keeps its
optimized concurrent batch path in ``FeedService`` and is NOT routed through
here. Every other source type (``json_api``, ``email_imap``, …) registers a
``SourceFetcher`` that turns a :class:`SourceRequest` into the same
:class:`~newsflow.core.feed_fetcher.FetchResult` the RSS path produces — so
dedupe, filtering, translation, digest and delivery downstream are identical
regardless of where an entry came from.

Implementations register themselves via :func:`register_source_fetcher` (lazily,
gated by their optional dependency, mirroring the rest of the codebase). The
registry is empty until those modules are imported.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from newsflow.core.feed_fetcher import FetchResult

logger = logging.getLogger(__name__)


@dataclass
class SourceRequest:
    """Everything a fetcher needs about one feed, decoupled from the ORM.

    ``config`` carries the source-specific settings stored on ``Feed.config``
    (JSONPath mappings, IMAP target, …). ``etag``/``last_modified`` are only
    meaningful for HTTP-polling sources and are ignored by the rest.
    """

    url: str
    etag: str | None = None
    last_modified: str | None = None
    config: dict | None = None


@runtime_checkable
class SourceFetcher(Protocol):
    """Fetch one source into a FetchResult. Must not raise for routine fetch
    failures — return ``FetchResult(success=False, error=...)`` instead, so one
    bad source can't abort the dispatch cycle."""

    async def fetch(self, req: SourceRequest) -> FetchResult: ...


_REGISTRY: dict[str, SourceFetcher] = {}

# Optional source modules, imported lazily on first use. Importing the module
# runs its register_source_fetcher() call. Kept lazy so a missing optional
# dependency surfaces as a per-fetch error, not a startup failure.
_OPTIONAL_SOURCE_MODULES = {
    "json_api": "newsflow.core.sources.json_api",
    "email_imap": "newsflow.core.sources.email_imap",
}


def register_source_fetcher(source_type: str, fetcher: SourceFetcher) -> None:
    """Register (or replace) the fetcher for a source type."""
    _REGISTRY[source_type] = fetcher


def get_source_fetcher(source_type: str | None) -> SourceFetcher | None:
    """Return the fetcher for ``source_type``, or None if none is registered.

    Lazily imports a known optional source module the first time its type is
    requested so it can self-register.
    """
    if not source_type:
        return None
    if source_type not in _REGISTRY and source_type in _OPTIONAL_SOURCE_MODULES:
        try:
            importlib.import_module(_OPTIONAL_SOURCE_MODULES[source_type])
        except Exception as e:
            logger.warning("Source type %r unavailable: %s", source_type, e)
    return _REGISTRY.get(source_type)


def known_source_types() -> set[str]:
    """Non-RSS source types that have a fetcher (installed or lazily
    importable). These are *pulled* by the dispatch loop."""
    return set(_OPTIONAL_SOURCE_MODULES)


# Source types that receive entries via push (the inbound API) instead of being
# polled. They have no SourceFetcher and must be skipped by the fetch loop, but
# are valid in declarative source config.
PUSH_SOURCE_TYPES = frozenset({"webhook_inbound"})


def declarable_source_types() -> set[str]:
    """All non-RSS source types valid in sources.yaml — pull fetchers plus push
    (inbound) types."""
    return set(_OPTIONAL_SOURCE_MODULES) | set(PUSH_SOURCE_TYPES)
