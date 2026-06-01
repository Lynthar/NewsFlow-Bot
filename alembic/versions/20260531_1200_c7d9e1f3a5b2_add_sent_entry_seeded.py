"""add sent_entries.seeded

Revision ID: c7d9e1f3a5b2
Revises: b2f4a6c8d0e1
Create Date: 2026-05-31 12:00:00+00:00

Distinguishes back-catalog rows written by ``seed_sent_entries`` (suppressed on
subscribe, NEVER shown to the channel) from real deliveries. The digest pipeline
reads SentEntry as "what the channel received"; without this flag, seeding a new
subscription's backlog polluted the next digest with articles the user never
saw. Existing rows predate seeding-vs-delivery tracking — backfill ``False``
(treat as delivered) via server_default so no real history is dropped from
digests; new rows get their value from the ORM ``default``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d9e1f3a5b2"
down_revision: Union[str, None] = "b2f4a6c8d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so the ADD COLUMN also works on older SQLite.
    with op.batch_alter_table("sent_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "seeded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("sent_entries", schema=None) as batch_op:
        batch_op.drop_column("seeded")
