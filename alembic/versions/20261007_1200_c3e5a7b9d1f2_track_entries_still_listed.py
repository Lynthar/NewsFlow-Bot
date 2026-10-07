"""track which sent entries the source still lists

Revision ID: c3e5a7b9d1f2
Revises: b8d0e2f4a6c8
Create Date: 2026-10-07 12:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e5a7b9d1f2'
down_revision: Union[str, None] = 'b8d0e2f4a6c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMN, not batch mode: nullable columns need no table rebuild.
    op.add_column(
        'sent_entries',
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'feeds',
        sa.Column('last_full_fetch_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table('feeds', schema=None) as batch_op:
        batch_op.drop_column('last_full_fetch_at')
    with op.batch_alter_table('sent_entries', schema=None) as batch_op:
        batch_op.drop_column('last_seen_at')
