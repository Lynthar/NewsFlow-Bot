"""Alembic migration environment.

Loads the database URL from `newsflow.config.Settings` and pulls model
metadata from `newsflow.models.base.Base` so autogenerate sees everything.

`render_as_batch=True` is required for SQLite to support ALTER-style changes
(adding/dropping columns on existing tables).
"""

import asyncio

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import models so their tables register on Base.metadata before autogenerate.
import newsflow.models.digest  # noqa: F401
import newsflow.models.feed  # noqa: F401
import newsflow.models.subscription  # noqa: F401
from newsflow.config import get_settings
from newsflow.models.base import Base

config = context.config

# Override the placeholder URL from alembic.ini with the real runtime URL.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Deliberately NOT calling logging.config.fileConfig(alembic.ini) here.
# main.py has already configured the root logger; fileConfig — even with
# disable_existing_loggers=False — would replace the root logger's
# handler and level with alembic.ini's [logger_root] (level=WARNING),
# silently dropping every INFO log emitted after upgrade_to_head() runs.
# alembic's own loggers (`alembic.runtime.migration`, etc.) still propagate
# to the root logger and print under main.py's format.

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
