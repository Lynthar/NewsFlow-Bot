"""add subscription message_template

Revision ID: f6a8b0c2d4e6
Revises: e5f7a9b1c3d5
Create Date: 2026-07-18 16:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a8b0c2d4e6'
down_revision: Union[str, None] = 'e5f7a9b1c3d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('message_template', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('message_template')
