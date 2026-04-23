"""add webhook_destinations table

Revision ID: e12acc4a473c
Revises: b5f9043fff02
Create Date: 2026-04-23 06:59:16+00:00

Introduces the webhook_destinations table. Named HTTP endpoints that the
WebhookAdapter posts feed entries to. Subscriptions reference a destination
by its `name` via Subscription.platform_channel_id (with platform="webhook").

Purely additive — no existing table touched. Bot upgrades that don't set a
webhooks.yaml path won't use the table at all.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e12acc4a473c'
down_revision: Union[str, None] = 'b5f9043fff02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webhook_destinations',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('format', sa.String(length=32), nullable=False),
        sa.Column('secret', sa.String(length=256), nullable=True),
        sa.Column('headers', sa.JSON(), nullable=True),
        sa.Column('timeout_s', sa.Integer(), nullable=False),
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_destinations')),
    )
    with op.batch_alter_table('webhook_destinations', schema=None) as batch_op:
        batch_op.create_index(
            'ix_webhook_destinations_name', ['name'], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table('webhook_destinations', schema=None) as batch_op:
        batch_op.drop_index('ix_webhook_destinations_name')

    op.drop_table('webhook_destinations')
