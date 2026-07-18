"""Offline configuration validator: ``python -m newsflow.checkconfig``.

Validates everything that can be checked without touching the network or the
database — the goal is to catch a bad deploy *before* the container enters a
crash loop:

- ``.env`` loads into Settings (pydantic validation, inline-comment poisoning,
  out-of-range values) and passes the same minimal-config check startup runs.
- ``webhooks.yaml`` / ``sources.yaml`` parse under the strict schemas
  (unknown keys are errors, same as startup).
- Cross-file: every ``platform: webhook`` subscriber in sources.yaml must
  reference a destination declared in webhooks.yaml — a dangling name would
  sync fine and then never deliver anything.

Exit code 0 = deployable (warnings allowed), 1 = at least one error.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from newsflow.config import Settings


def _check(errors: list[str], warnings: list[str], infos: list[str]) -> None:
    """Run all validations, appending findings to the given lists.

    Imports happen inside so a broken installation reports as a finding
    instead of a bare traceback.
    """
    from pydantic import ValidationError

    from newsflow.config import Settings

    try:
        settings = Settings()
    except ValidationError as e:
        errors.append(f".env failed validation:\n{e}")
        return

    # Scheme only — a Postgres URL carries credentials.
    backend = settings.database_url.split(":", 1)[0]
    infos.append(f"database backend: {backend}")

    tokens = []
    if settings.discord_token:
        tokens.append("discord")
    if settings.telegram_token:
        tokens.append("telegram")
    if tokens:
        infos.append(f"platform tokens present: {', '.join(tokens)}")
    else:
        errors.append(
            "no platform token set (DISCORD_TOKEN / TELEGRAM_TOKEN) — startup would abort"
        )

    if settings.translation_enabled and not settings.can_translate():
        warnings.append(
            f"translation_enabled but provider {settings.translation_provider!r} "
            "has no API key — entries will be delivered untranslated"
        )

    if settings.api_enabled and not settings.api_key:
        warnings.append(
            "api_enabled without API_KEY — write endpoints and /api/ingest "
            "are fail-closed (503), push sources won't work"
        )

    _check_webhooks_yaml(settings, errors, infos)
    _check_sources_yaml(settings, errors, infos)


def _check_webhooks_yaml(settings: Settings, errors: list[str], infos: list[str]) -> None:
    from newsflow.services.webhook_sync import WebhookConfigError, parse_webhooks_yaml

    path = settings.webhooks_config_path
    if not path.is_file():
        infos.append(f"webhooks.yaml: not present at {path} (webhook delivery disabled)")
        return
    try:
        cfg = parse_webhooks_yaml(path)
    except WebhookConfigError as e:
        errors.append(f"webhooks.yaml: {e}")
        return
    infos.append(
        f"webhooks.yaml: {len(cfg.destinations)} destination(s), "
        f"{sum(len(v) for v in cfg.subscriptions.values())} subscription(s)"
    )


def _check_sources_yaml(settings: Settings, errors: list[str], infos: list[str]) -> None:
    from newsflow.services.source_sync import SourceConfigError, parse_sources_yaml
    from newsflow.services.webhook_sync import WebhookConfigError, parse_webhooks_yaml

    path = settings.sources_config_path
    if not path.is_file():
        infos.append(f"sources.yaml: not present at {path} (no declarative sources)")
        return
    try:
        sources = parse_sources_yaml(path)
    except SourceConfigError as e:
        errors.append(f"sources.yaml: {e}")
        return
    infos.append(f"sources.yaml: {len(sources)} source(s)")

    # Cross-file: webhook subscribers must point at declared destinations.
    webhook_refs = {
        (src.name, sub.channel)
        for src in sources
        for sub in src.subscribers
        if sub.platform == "webhook"
    }
    if not webhook_refs:
        return
    known: set[str] = set()
    wh_path = settings.webhooks_config_path
    if wh_path.is_file():
        try:
            known = set(parse_webhooks_yaml(wh_path).destinations)
        except WebhookConfigError:
            return  # already reported as its own error above
    for source_name, dest in sorted(webhook_refs):
        if dest not in known:
            errors.append(
                f"sources.yaml: source {source_name!r} subscribes webhook "
                f"destination {dest!r}, which webhooks.yaml does not declare "
                "— it would sync but never deliver"
            )


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []
    _check(errors, warnings, infos)

    for line in infos:
        print(f"   {line}")
    for line in warnings:
        print(f"!  {line}")
    for line in errors:
        print(f"X  {line}")

    if errors:
        print(f"\nFAIL - {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"\nOK - config is deployable ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
