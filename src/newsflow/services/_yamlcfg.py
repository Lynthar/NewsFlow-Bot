"""Validators shared by the sources.yaml and webhooks.yaml parsers. Each parser
passes its own error class: callers catch SourceConfigError / WebhookConfigError
by name, so nothing here may raise a common base instead."""

from __future__ import annotations

from dataclasses import fields
from typing import Any


def yaml_keys(cls: type[Any]) -> frozenset[str]:
    """The keys a YAML block may carry: the field names of the dataclass it parses into."""
    return frozenset(f.name for f in fields(cls))


def reject_unknown_keys(
    exc: type[Exception], context: str, cfg: dict[Any, Any], allowed: frozenset[str]
) -> None:
    unknown = sorted(str(k) for k in cfg.keys() if k not in allowed)
    if unknown:
        raise exc(f"{context}: unknown key(s) {unknown}. Allowed: {sorted(allowed)}")


def require_bool(exc: type[Exception], context: str, key: str, value: Any, default: bool) -> bool:
    """YAML booleans must actually BE booleans — `bool("false")` is True (non-empty
    string), silently inverting the operator's intent."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise exc(
        f"{context}: `{key}` must be a YAML boolean (true/false), got {value!r} "
        f'— remove the quotes if you wrote "false"'
    )
