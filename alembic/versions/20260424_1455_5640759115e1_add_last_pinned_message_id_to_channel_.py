"""add last_pinned_message_id to channel_digests

Revision ID: 5640759115e1
Revises: e12acc4a473c
Create Date: 2026-04-24 14:55:00+00:00

Supports auto-pin of the latest digest: the dispatcher records the
platform message id of the pinned digest so the next delivery can
unpin the previous one before pinning the new. Purely additive —
nullable column, no data backfill, existing rows default to NULL
until the next digest delivery writes a value.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5640759115e1'
down_revision: Union[str, None] = 'e12acc4a473c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so SQLite gets a proper ALTER via table-copy.
    with op.batch_alter_table('channel_digests', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'last_pinned_message_id', sa.String(length=64), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('channel_digests', schema=None) as batch_op:
        batch_op.drop_column('last_pinned_message_id')
