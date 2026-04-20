"""Alembic upgrade helper for startup.

Keeps the sync alembic API off the main asyncio event loop by running the
command in a worker thread (where alembic is free to spin its own loop via
env.py's `asyncio.run(run_migrations_online())`).
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _alembic_ini_path() -> Path:
    """Locate alembic.ini at the project root.

    Works in dev (CWD = repo root) and in the Docker image (WORKDIR = /app).
    Resolution is relative to this file so CWD doesn't matter.
    """
    # models/migrate.py → models → newsflow → src → <repo root>
    return Path(__file__).resolve().parents[3] / "alembic.ini"


def _upgrade_sync() -> None:
    from alembic import command
    from alembic.config import Config

    ini = _alembic_ini_path()
    if not ini.exists():
        raise FileNotFoundError(
            f"alembic.ini not found at {ini} — migrations cannot run"
        )
    command.upgrade(Config(str(ini)), "head")


async def upgrade_to_head() -> None:
    """Apply all pending Alembic migrations. Safe to call from asyncio code."""
    logger.info("Running Alembic migrations (upgrade to head)...")
    await asyncio.to_thread(_upgrade_sync)
    logger.info("Alembic migrations complete")
