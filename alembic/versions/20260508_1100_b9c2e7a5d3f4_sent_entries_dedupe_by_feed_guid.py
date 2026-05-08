"""sent_entries: dedupe by (feed_id, guid) instead of FK to feed_entries.id

Revision ID: b9c2e7a5d3f4
Revises: a4f1d92e7c83
Create Date: 2026-05-08 11:00:00+00:00

Why this exists
---------------

The old schema put a FK on `sent_entries.entry_id -> feed_entries.id`
with `ondelete=CASCADE`. That meant the dedupe signal ("this channel
already saw this article") was tied to the existence of a particular
FeedEntry row. When `cleanup_old_entries` deleted a FeedEntry — for any
reason: age, or a race during the cleanup loop — the matching SentEntry
got cascade-deleted with it. Then the next fetch re-ingested the same
article (different FeedEntry.id, same feed_id+guid), the dispatcher saw
no matching SentEntry, and pushed it again. Users observed bursts of
re-delivered articles that they had already seen.

After this migration SentEntry stores `(feed_id, guid)` as the natural
identifier instead of an FK to FeedEntry.id. cleanup of FeedEntry no
longer drops the dedupe signal. Re-ingestion of the same GUID is
recognized as already-seen.

`subscription_id` keeps its CASCADE FK so unsubscribing or deleting a
subscription still wipes its SentEntry rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b9c2e7a5d3f4"
down_revision: Union[str, None] = "a4f1d92e7c83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: add new columns as nullable so the backfill can run without
    # tripping NOT NULL on rows that don't have values yet.
    with op.batch_alter_table("sent_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("feed_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("guid", sa.String(length=2048), nullable=True)
        )

    # Step 2: backfill (feed_id, guid) from feed_entries via the old
    # entry_id FK. Rows whose entry_id no longer points at a live
    # FeedEntry (shouldn't exist thanks to the old CASCADE, but possible
    # if an earlier crash left stragglers) will get NULL here and be
    # dropped in step 3.
    op.execute(
        """
        UPDATE sent_entries
        SET feed_id = (
                SELECT feed_id FROM feed_entries
                WHERE feed_entries.id = sent_entries.entry_id
            ),
            guid = (
                SELECT guid FROM feed_entries
                WHERE feed_entries.id = sent_entries.entry_id
            )
        """
    )

    # Step 3: drop orphans — these would violate the upcoming NOT NULL
    # and have no useful information anyway (the FeedEntry they pointed
    # at is already gone, so we can't reconstruct a dedupe signal).
    op.execute(
        "DELETE FROM sent_entries WHERE feed_id IS NULL OR guid IS NULL"
    )

    # Step 4: lock down the new columns, drop the old entry_id column +
    # its unique index, and create the new unique index keyed on
    # (subscription_id, feed_id, guid).
    #
    # batch_alter_table on SQLite implements all of this by rebuilding
    # the table — the old FK to feed_entries.id is dropped along with
    # the entry_id column. The subscription FK (ondelete=CASCADE) is
    # preserved: SQLAlchemy reads the existing FK from the reflected
    # schema and re-applies it on the new table.
    with op.batch_alter_table("sent_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_sent_entries_subscription_entry")
        batch_op.drop_column("entry_id")
        batch_op.alter_column(
            "feed_id", existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "guid", existing_type=sa.String(length=2048), nullable=False
        )
        batch_op.create_index(
            "ix_sent_entries_sub_feed_guid",
            ["subscription_id", "feed_id", "guid"],
            unique=True,
        )


def downgrade() -> None:
    # Best-effort downgrade: re-create entry_id, look up FeedEntry by
    # (feed_id, guid). SentEntry rows whose FeedEntry has since been
    # cleaned up cannot be re-keyed and are dropped — that's the price
    # of going back to the old FK-based design.
    with op.batch_alter_table("sent_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("entry_id", sa.Integer(), nullable=True))

    op.execute(
        """
        UPDATE sent_entries
        SET entry_id = (
            SELECT id FROM feed_entries
            WHERE feed_entries.feed_id = sent_entries.feed_id
              AND feed_entries.guid = sent_entries.guid
        )
        """
    )
    op.execute("DELETE FROM sent_entries WHERE entry_id IS NULL")

    with op.batch_alter_table("sent_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_sent_entries_sub_feed_guid")
        batch_op.drop_column("guid")
        batch_op.drop_column("feed_id")
        batch_op.alter_column(
            "entry_id", existing_type=sa.Integer(), nullable=False
        )
        batch_op.create_foreign_key(
            "fk_sent_entries_entry_id_feed_entries",
            "feed_entries",
            ["entry_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_sent_entries_subscription_entry",
            ["subscription_id", "entry_id"],
            unique=True,
        )
