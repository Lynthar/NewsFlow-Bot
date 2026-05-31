"""add feeds.source_type and feeds.config

Revision ID: b2f4a6c8d0e1
Revises: a7f3c9e21b06
Create Date: 2026-05-31 10:00:00+00:00

Additive columns for multi-source support (Phase 1). ``source_type`` selects
the fetcher — the ``'rss'`` server_default keeps every existing feed on the RSS
path, so this is fully backward-compatible. ``config`` holds source-specific
settings (JSONPath mappings, IMAP target, …) as JSON: TEXT on SQLite, JSONB on
Postgres. Existing rows get ``source_type='rss'`` and ``config=NULL``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2f4a6c8d0e1"
down_revision: Union[str, None] = "a7f3c9e21b06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            server_default="rss",
        ),
    )
    op.add_column("feeds", sa.Column("config", sa.JSON(), nullable=True))


def downgrade() -> None:
    # batch_alter_table so the column drop also works on older SQLite.
    with op.batch_alter_table("feeds") as batch:
        batch.drop_column("config")
        batch.drop_column("source_type")
