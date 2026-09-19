"""Shared pytest fixtures.

Production code reaches configuration through ``get_settings()`` and the
database through ``get_session_factory()``, both process-wide singletons. The
fixtures here point those singletons at test-owned values instead of patching
the names that import them, so a test walks the same lookup path as the bot.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from newsflow.config import Settings, get_settings
from newsflow.models import base as db_base
from newsflow.models.base import Base, close_db, get_session_factory, init_db


def _env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)


@pytest.fixture(autouse=True)
def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real ``Settings``, built from the environment the way the process builds it.

    Autouse baseline: the repo's ``.env`` is not read, the database URL points into
    ``tmp_path`` (so ``data_dir`` does too) and the ``get_settings()`` cache is cleared
    before and after the test. Call ``configure(field=value, ...)`` to export more
    fields as environment variables; every later ``get_settings()`` anywhere in the
    code sees them. Unknown field names fail here rather than being ignored.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    def _configure(**fields: object) -> Settings:
        for name, value in fields.items():
            assert name in Settings.model_fields, f"Settings has no field {name!r}"
            if value is None:
                monkeypatch.delenv(name.upper(), raising=False)
            else:
                monkeypatch.setenv(name.upper(), _env_value(value))
        get_settings.cache_clear()
        return get_settings()

    _configure(database_url=f"sqlite+aiosqlite:///{tmp_path / 'newsflow.db'}")
    yield _configure
    get_settings.cache_clear()
    # An engine left open would carry this test's database into the next one;
    # a test that opens it must take the ``db`` fixture, which closes it.
    assert db_base._engine is None, "database engine left open — use the `db` fixture"


@pytest_asyncio.fixture
async def db(configure):
    """A fresh SQLite file behind the process-wide engine: ``get_session_factory()``
    in production code hands out sessions on it. Yields that factory, for seeding
    rows and reading state back."""
    await close_db()
    await init_db()
    yield get_session_factory()
    await close_db()


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """In-memory SQLite session with schema created. One engine per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as s:
        yield s

    await engine.dispose()
