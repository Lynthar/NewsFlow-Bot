"""Alembic migrations run against a SQLite file that already holds data.

On SQLite, batch mode rebuilds a table by copying it and dropping the original.
Every row that references the dropped table must survive that, above all the
``sent_entries`` dedupe history, which has no second source.
"""

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

# The schema at this revision predates the silent column and the (feed_id, guid)
# dedupe key, so the way to head rebuilds both subscriptions and sent_entries.
OLD_REVISION = "5640759115e1"

TS = "2026-04-25 00:00:00"


def _seed_old_schema(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO feeds (id, url, is_active, error_count, created_at, updated_at)"
            " VALUES (1, 'https://example.com/feed', 1, 0, ?, ?)",
            (TS, TS),
        )
        conn.executemany(
            "INSERT INTO feed_entries (id, feed_id, guid, title, link, created_at, updated_at)"
            " VALUES (?, 1, ?, 'T', 'https://example.com/a', ?, ?)",
            [(i, f"g{i}", TS, TS) for i in (1, 2)],
        )
        conn.executemany(
            "INSERT INTO subscriptions (id, platform, platform_user_id, platform_channel_id,"
            " feed_id, is_active, translate, target_language, show_summary, show_image,"
            " created_at, updated_at) VALUES (?, 'discord', 'u', ?, 1, 1, 0, 'en', 1, 1, ?, ?)",
            [(i, f"c{i}", TS, TS) for i in (1, 2)],
        )
        conn.executemany(
            "INSERT INTO sent_entries (id, subscription_id, entry_id, sent_at, was_filtered,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            [(1, 1, 1, TS, TS, TS), (2, 1, 2, TS, TS, TS), (3, 2, 1, TS, TS, TS)],
        )


def _counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(db) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("feeds", "feed_entries", "subscriptions", "sent_entries")
        }


def _fk_violations(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        return conn.execute("PRAGMA foreign_key_check").fetchall()


def test_upgrade_and_downgrade_keep_every_row(
    configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "migrate.db"
    configure(database_url=f"sqlite+aiosqlite:///{db}")
    # Startup runs migrations from whatever directory the process was launched in.
    monkeypatch.chdir(tmp_path)
    cfg = Config(str(ALEMBIC_INI))

    command.upgrade(cfg, OLD_REVISION)
    _seed_old_schema(db)
    seeded = _counts(db)
    assert seeded == {"feeds": 1, "feed_entries": 2, "subscriptions": 2, "sent_entries": 3}

    command.upgrade(cfg, "head")
    assert _counts(db) == seeded

    command.downgrade(cfg, OLD_REVISION)
    assert _counts(db) == seeded

    command.upgrade(cfg, "head")
    assert _counts(db) == seeded
    assert _fk_violations(db) == []
