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

# Table registration happens in newsflow.models.__init__, which imports every
# model module. Don't list individual modules here: a hand-maintained copy
# drifts and reads as if it were the registration point.
import newsflow.models  # noqa: F401
from newsflow.config import get_settings
from newsflow.models.base import Base

config = context.config

# Override the placeholder URL from alembic.ini with the real runtime URL.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Deliberately NOT calling fileConfig(alembic.ini): main.py already configured the
# root logger, and fileConfig would replace its handler and level with
# [logger_root] WARNING, dropping every INFO emitted after upgrade_to_head().

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
    if connection.dialect.name == "sqlite":
        # Batch mode rebuilds a table by DROP + rename; with foreign keys on, the DROP
        # cascades and empties every child table. SQLite ignores the pragma inside a
        # transaction, so it is committed before alembic opens one.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
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
        hide_parameters=not get_settings().db_echo,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
