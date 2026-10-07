"""Validators shared by the sources.yaml and webhooks.yaml parsers. Each parser
passes its own error class: callers catch SourceConfigError / WebhookConfigError
by name, so nothing here may raise a common base instead."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import String
from sqlalchemy.orm import QueryableAttribute

from newsflow.core.languages import LANGUAGE_CODE_EXAMPLES, normalize_language_code


def load_yaml(exc: type[Exception], path: Path) -> Any:
    """Parse `path`, raising `exc` if it can't be read or parsed. Parsed from the open
    file, never its text: from a str, PyYAML quotes the source lines around an error,
    and that line can hold a secret or a tokenised URL."""
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError as e:
        raise exc(f"couldn't read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise exc(f"malformed YAML in {path}: {e}") from e


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


def require_fits(
    exc: type[Exception], context: str, key: str, value: str, column: QueryableAttribute[Any]
) -> str:
    """`value` when it fits the column it is stored in. Longer, SQLite keeps it whole and
    Postgres rejects the whole sync, crashing startup without naming the key."""
    column_type = column.type
    assert isinstance(column_type, String) and column_type.length is not None
    if len(value) > column_type.length:
        raise exc(f"{context}: `{key}` is longer than {column_type.length} characters")
    return value


def require_language(exc: type[Exception], context: str, value: Any, default: str) -> str:
    """The language code normalized the way every command stores it (zh-cn → zh-CN)."""
    if value is None:
        return default
    code = normalize_language_code(str(value))
    if code is None:
        raise exc(
            f"{context}: `language` {value!r} is not a language code "
            f"(e.g. {LANGUAGE_CODE_EXAMPLES})"
        )
    return code
