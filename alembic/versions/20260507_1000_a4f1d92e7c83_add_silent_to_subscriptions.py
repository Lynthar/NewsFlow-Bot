"""add silent column to subscriptions

Revision ID: a4f1d92e7c83
Revises: 5640759115e1
Create Date: 2026-05-07 10:00:00+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a4f1d92e7c83"
down_revision: Union[str, None] = "5640759115e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing subscription rows pre-date this column; backfill with False
    # via server_default so the NOT NULL constraint holds. New rows get
    # their value from the ORM `default=False`.
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "silent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.drop_column("silent")
