"""add channel_settings table

Revision ID: d4e6f8a0b2c4
Revises: c7d9e1f3a5b2
Create Date: 2026-07-16 13:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e6f8a0b2c4'
down_revision: Union[str, None] = 'c7d9e1f3a5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('channel_settings',
    sa.Column('platform', sa.String(length=20), nullable=False),
    sa.Column('platform_channel_id', sa.String(length=64), nullable=False),
    sa.Column('default_language', sa.String(length=10), nullable=True),
    sa.Column('default_translate', sa.Boolean(), nullable=True),
    sa.Column('default_silent', sa.Boolean(), nullable=True),
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_channel_settings'))
    )
    with op.batch_alter_table('channel_settings', schema=None) as batch_op:
        batch_op.create_index(
            'ix_channel_settings_platform_channel',
            ['platform', 'platform_channel_id'],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('channel_settings', schema=None) as batch_op:
        batch_op.drop_index('ix_channel_settings_platform_channel')

    op.drop_table('channel_settings')
