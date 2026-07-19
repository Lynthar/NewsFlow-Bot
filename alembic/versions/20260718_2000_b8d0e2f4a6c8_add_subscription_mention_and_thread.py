"""add subscription mention and message_thread_id

Revision ID: b8d0e2f4a6c8
Revises: f6a8b0c2d4e6
Create Date: 2026-07-18 20:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d0e2f4a6c8'
down_revision: Union[str, None] = 'f6a8b0c2d4e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('mention', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('message_thread_id', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('message_thread_id')
        batch_op.drop_column('mention')
