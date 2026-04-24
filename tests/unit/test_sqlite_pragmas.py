"""Smoke test: SQLite engine has WAL mode + foreign keys + sane sync.

The connect-event listener in models.base sets these per-connection.
If it's ever accidentally unregistered or narrowed wrong, concurrent
writes (dispatch loop vs interactive command) start failing with
"database is locked" — the exact bug this was added to fix. Keeping a
test so that regression is loud.
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

# Importing this registers the connect-event listener on the global
# Engine class. The listener is module-scoped, so simply importing the
# module is enough.
import newsflow.models.base  # noqa: F401


async def _read_pragma(engine, name: str):
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(f"PRAGMA {name}")
        row = result.fetchone()
        return row[0] if row else None


async def test_sqlite_pragmas_applied_on_connect(tmp_path: Path) -> None:
    """File-backed SQLite, so WAL is actually applied (WAL is a no-op
    on :memory: — SQLite silently falls back to 'memory' journal mode
    since WAL needs a real file for the -wal sidecar)."""
    db_path = tmp_path / "newsflow-pragma-test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", future=True
    )
    try:
        mode = await _read_pragma(engine, "journal_mode")
        assert str(mode).lower() == "wal", (
            f"expected WAL journal mode, got {mode!r}"
        )

        # synchronous: 0=OFF, 1=NORMAL, 2=FULL. We set NORMAL.
        sync = await _read_pragma(engine, "synchronous")
        assert int(sync) == 1, f"expected synchronous=NORMAL (1), got {sync}"

        fk = await _read_pragma(engine, "foreign_keys")
        assert int(fk) == 1, f"expected foreign_keys=ON, got {fk}"
    finally:
        await engine.dispose()


async def test_memory_sqlite_does_not_raise_even_though_wal_is_noop() -> None:
    """The pragma set includes WAL, which isn't supported on :memory:.
    Previously our connect hook was FK-only; now it runs WAL too, and
    SQLite silently falls back to a different journal mode. Make sure
    this doesn't raise — :memory: is the fixture backend used by every
    other test in the suite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    try:
        # Just connecting is the check; if the pragma ran and silently
        # fell back, we're fine. If it raised, the engine.connect()
        # itself would have thrown.
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("SELECT 1")
            assert result.fetchone()[0] == 1
    finally:
        await engine.dispose()
