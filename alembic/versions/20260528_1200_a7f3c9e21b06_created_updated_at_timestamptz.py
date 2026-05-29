"""created_at/updated_at -> TIMESTAMP WITH TIME ZONE (Postgres)

Revision ID: a7f3c9e21b06
Revises: b9c2e7a5d3f4
Create Date: 2026-05-28 12:00:00+00:00

Why this exists
---------------

`Base` injects `created_at`/`updated_at` into every model with a tz-AWARE
default (`datetime.now(timezone.utc)`), but the initial migration created
them as `sa.DateTime()` = `TIMESTAMP WITHOUT TIME ZONE`. On Postgres,
asyncpg refuses to encode an aware datetime into a naive timestamp column
and raises `DataError: can't subtract offset-naive and offset-aware
datetimes`, so the very first INSERT into any table failed on the
Postgres backend. Every other timestamp column in the schema already uses
`DateTime(timezone=True)`; this aligns these two columns with that.

This migration is a no-op on SQLite (and any non-Postgres backend):
SQLite has no real `timestamptz` type and stores datetimes as ISO text,
so the `DateTime(timezone=True)` flag never mattered there and the
existing columns need no change. Only Postgres requires the column type
to actually be `timestamptz`.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f3c9e21b06"
down_revision: Union[str, None] = "b9c2e7a5d3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every table inherits created_at/updated_at from Base.
_TABLES = (
    "feeds",
    "feed_entries",
    "subscriptions",
    "sent_entries",
    "channel_digests",
    "webhook_destinations",
)
_COLUMNS = ("created_at", "updated_at")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite et al. store datetimes without a real timezone type, so
        # there is nothing to migrate; the bug only manifests on asyncpg.
        return
    for table in _TABLES:
        for col in _COLUMNS:
            # Existing values were written as UTC, so interpret the naive
            # stored value as UTC when widening to timestamptz.
            op.execute(
                f"ALTER TABLE {table} "
                f"ALTER COLUMN {col} TYPE TIMESTAMP WITH TIME ZONE "
                f"USING {col} AT TIME ZONE 'UTC'"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _TABLES:
        for col in _COLUMNS:
            op.execute(
                f"ALTER TABLE {table} "
                f"ALTER COLUMN {col} TYPE TIMESTAMP WITHOUT TIME ZONE "
                f"USING {col} AT TIME ZONE 'UTC'"
            )
