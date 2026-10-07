"""Runtime reload of the declarative YAML configs (webhooks.yaml / sources.yaml).

Re-runs the same idempotent syncs main.py runs at startup, on SIGHUP (Unix) or
``POST /api/admin/reload``.

Failure semantics differ from startup: a bad file at boot aborts, a bad file at reload
keeps the PREVIOUS synced state (both syncs parse fully before touching the DB) and
reports the error to the caller. The two files reload independently.

An absent file skips that sync and keeps its state; sources.yaml's sources stay active
until the file is emptied to ``sources: {}``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from newsflow.config import get_settings
from newsflow.services import get_dispatcher

logger = logging.getLogger(__name__)

# Serialises concurrent reload triggers (SIGHUP burst, repeated API calls) —
# the syncs are idempotent but interleaving two runs would thrash the DB.
_reload_lock = asyncio.Lock()


@dataclass
class ReloadResult:
    ok: bool
    detail: str


async def reload_declarative_configs() -> ReloadResult:
    """Re-run webhook_sync + source_sync and refresh the webhook adapter's
    destination cache. Returns ok=False (with every error collected) when any
    file failed to parse; that file's previous state stays in effect."""
    settings = get_settings()
    async with _reload_lock:
        applied: list[str] = []
        errors: list[str] = []

        if settings.webhooks_enabled:
            from newsflow.services.webhook_sync import WebhookConfigError, sync_webhooks

            try:
                await sync_webhooks(settings.webhooks_config_path)
                applied.append("webhooks.yaml synced")
            except WebhookConfigError as e:
                errors.append(f"webhooks.yaml: {e}")
        else:
            applied.append("webhooks.yaml absent (skipped)")

        if settings.sources_enabled:
            from newsflow.services.source_sync import SourceConfigError, sync_sources

            try:
                await sync_sources(settings.sources_config_path)
                applied.append("sources.yaml synced")
            except SourceConfigError as e:
                errors.append(f"sources.yaml: {e}")
        else:
            applied.append("sources.yaml absent (skipped)")

        # The webhook adapter caches destinations in memory; refresh so URL /
        # header edits take effect without waiting for a restart.
        adapter = get_dispatcher().get_adapter("webhook")
        reload_fn = getattr(adapter, "reload_destinations", None)
        if reload_fn is not None:
            await reload_fn()
            applied.append("webhook destinations cache refreshed")

        detail = "; ".join(errors + applied)
        if errors:
            logger.error(f"config reload finished with errors: {detail}")
            return ReloadResult(ok=False, detail=detail)
        logger.info(f"config reload OK: {detail}")
        return ReloadResult(ok=True, detail=detail)
