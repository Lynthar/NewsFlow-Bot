"""add webhook destination health columns

Revision ID: e5f7a9b1c3d5
Revises: d4e6f8a0b2c4
Create Date: 2026-07-18 09:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f7a9b1c3d5'
down_revision: Union[str, None] = 'd4e6f8a0b2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'webhook_destinations',
        sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        'webhook_destinations',
        sa.Column('error_count', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column(
        'webhook_destinations',
        sa.Column('last_error', sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table('webhook_destinations', schema=None) as batch_op:
        batch_op.drop_column('last_error')
        batch_op.drop_column('error_count')
        batch_op.drop_column('is_active')
