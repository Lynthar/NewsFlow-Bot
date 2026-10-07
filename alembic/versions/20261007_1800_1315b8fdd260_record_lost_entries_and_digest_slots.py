"""record lost entries and served digest slots

Revision ID: 1315b8fdd260
Revises: c3e5a7b9d1f2
Create Date: 2026-10-07 18:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1315b8fdd260'
down_revision: Union[str, None] = 'c3e5a7b9d1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMN, not batch mode: a nullable column or a constant default needs
    # no table rebuild.
    op.add_column(
        'sent_entries',
        sa.Column('undeliverable', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'subscriptions',
        sa.Column('dropped_unsent', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'channel_digests',
        sa.Column('last_slot_at', sa.DateTime(timezone=True), nullable=True),
    )
    # A delivery always followed the slot it served, so its time marks that slot served.
    op.execute('UPDATE channel_digests SET last_slot_at = last_delivered_at')


def downgrade() -> None:
    with op.batch_alter_table('channel_digests', schema=None) as batch_op:
        batch_op.drop_column('last_slot_at')
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('dropped_unsent')
    with op.batch_alter_table('sent_entries', schema=None) as batch_op:
        batch_op.drop_column('undeliverable')
