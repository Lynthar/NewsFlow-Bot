"""drop unused last_sent_at / last_entry_guid from subscriptions

Revision ID: b5f9043fff02
Revises: 8e1b84612c65
Create Date: 2026-04-23 06:26:38+00:00

These two columns were carried forward from an earlier design where each
Subscription tracked its own "last delivered" cursor. The dispatcher never
wrote to them — tracking happens in the SentEntry table — so they were
always NULL and purely dead weight. Dropping them removes that confusion
for future readers.

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b5f9043fff02'
down_revision: Union[str, None] = '8e1b84612c65'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so SQLite also gets a proper ALTER via table-copy.
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('last_entry_guid')
        batch_op.drop_column('last_sent_at')


def downgrade() -> None:
    # Re-add as nullable so rollback works even though the columns are
    # never populated by application code.
    import sqlalchemy as sa

    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column('last_entry_guid', sa.String(length=2048), nullable=True)
        )
