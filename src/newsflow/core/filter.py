"""Subscription filter rules.

A rule narrows what a single Subscription actually receives:

- `include_keywords`: the entry must contain at least one (case-insensitive
  substring match on title + summary combined)
- `exclude_keywords`: if any appears, the entry is dropped

An empty rule (no keywords either way) matches everything, i.e. no filter.
Rules are stored on Subscription.filter_rule as JSON.

Matching is a pure function — no I/O, no DB — so it's cheap enough to
re-evaluate each entry on every dispatch cycle without caching.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FilterRule:
    include_keywords: tuple[str, ...] = field(default_factory=tuple)
    exclude_keywords: tuple[str, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return not self.include_keywords and not self.exclude_keywords

    def matches(self, text: str) -> bool:
        """Return True if `text` passes the filter.

        Matching is case-insensitive substring. `text` is typically
        `f"{entry.title} {entry.summary}"`, built by the caller.
        """
        if not text:
            text = ""
        lowered = text.lower()

        if self.include_keywords:
            if not any(kw.lower() in lowered for kw in self.include_keywords):
                return False

        if self.exclude_keywords:
            if any(kw.lower() in lowered for kw in self.exclude_keywords):
                return False

        return True

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "FilterRule":
        if not data:
            return cls()
        return cls(
            include_keywords=tuple(data.get("include_keywords") or ()),
            exclude_keywords=tuple(data.get("exclude_keywords") or ()),
        )

    def to_json(self) -> dict[str, list[str]] | None:
        """Serialize for DB storage. Empty rules round-trip to None so we
        don't persist a row cluttered with empty arrays."""
        if self.is_empty():
            return None
        return {
            "include_keywords": list(self.include_keywords),
            "exclude_keywords": list(self.exclude_keywords),
        }


def parse_keyword_csv(raw: str | None) -> tuple[str, ...]:
    """Parse a user-supplied comma-separated keyword string.

    Trims whitespace on each token, drops empties. `None` or `""` → empty tuple.
    """
    if not raw:
        return ()
    return tuple(
        kw for kw in (item.strip() for item in raw.split(",")) if kw
    )
