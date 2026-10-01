"""Publishing by request, and dismissals that belong to a person.

Revision ID: 0017_publish_approval
Revises: 0016_lists
Create Date: 2026-10-01

Two changes that ship together because the second is what makes the first
worth having.

**Approval.** An analyst's publish used to take effect at once. It is now a
request that an administrator approves or rejects; ``query_charts`` records who
asked and when, and the last rejection with its reason. Nothing is backfilled:
every chart that is published stays published, every other chart starts private.

**Personal dismissals.** ``flag_dismissals`` was one row per (query, row), so
whoever dismissed a finding cleared it for everybody. Once a published chart's
alerts reach the people it was shared with, that is wrong in both directions:
a viewer would hide the author's findings, and the author would hide the
viewer's. A dismissal now belongs to a user.

Existing rows are handed to the owner of their query (that is who could dismiss
before), or to the first administrator when the query has no owner, and a row
with neither is dropped: with nobody to attribute it to it would hide a finding
from nobody.

``user_id`` is CASCADE: a dismissal is somebody's own reading state. The
publication columns are RESTRICT like ``published_by`` in 0015: a request or a
rejection that has forgotten who made it answers nothing.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_publish_approval"
down_revision = "0016_lists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("query_charts") as batch:
        batch.add_column(sa.Column("publish_requested_by", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("publish_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("publish_rejected_by", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("publish_rejected_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("publish_rejected_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_query_charts_publish_requested_by",
            "users",
            ["publish_requested_by"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_query_charts_publish_rejected_by",
            "users",
            ["publish_rejected_by"],
            ["id"],
            ondelete="RESTRICT",
        )
    # The approvals queue reads exactly this: pending charts, oldest first.
    op.create_index(
        "ix_query_charts_publish_requested_at", "query_charts", ["publish_requested_at"]
    )

    with op.batch_alter_table("flag_dismissals") as batch:
        batch.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))

    # Backfill before the column is made NOT NULL. Owner first, then the
    # earliest administrator, then nobody (dropped below).
    op.execute(
        "UPDATE flag_dismissals SET user_id = ("
        "SELECT owner_id FROM saved_queries WHERE saved_queries.id = flag_dismissals.query_id)"
    )
    op.execute(
        "UPDATE flag_dismissals SET user_id = ("
        "SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1) "
        "WHERE user_id IS NULL"
    )
    op.execute("DELETE FROM flag_dismissals WHERE user_id IS NULL")

    with op.batch_alter_table("flag_dismissals") as batch:
        batch.alter_column("user_id", existing_type=sa.String(length=36), nullable=False)
        batch.drop_constraint("uq_flag_dismissals_row", type_="unique")
        batch.create_foreign_key(
            "fk_flag_dismissals_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_unique_constraint(
            "uq_flag_dismissals_row", ["query_id", "user_id", "row_fingerprint"]
        )
    op.create_index("ix_flag_dismissals_user_id", "flag_dismissals", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_flag_dismissals_user_id", table_name="flag_dismissals")
    # Several people can have dismissed the same row; the old key allows one.
    # Keep the earliest, drop the rest, or the unique constraint cannot be rebuilt.
    op.execute(
        "DELETE FROM flag_dismissals WHERE id NOT IN ("
        "SELECT MIN(id) FROM flag_dismissals GROUP BY query_id, row_fingerprint)"
    )
    with op.batch_alter_table("flag_dismissals") as batch:
        batch.drop_constraint("uq_flag_dismissals_row", type_="unique")
        batch.drop_constraint("fk_flag_dismissals_user_id", type_="foreignkey")
        batch.drop_column("user_id")
        batch.create_unique_constraint("uq_flag_dismissals_row", ["query_id", "row_fingerprint"])

    op.drop_index("ix_query_charts_publish_requested_at", table_name="query_charts")
    with op.batch_alter_table("query_charts") as batch:
        batch.drop_constraint("fk_query_charts_publish_rejected_by", type_="foreignkey")
        batch.drop_constraint("fk_query_charts_publish_requested_by", type_="foreignkey")
        batch.drop_column("publish_rejected_reason")
        batch.drop_column("publish_rejected_at")
        batch.drop_column("publish_rejected_by")
        batch.drop_column("publish_requested_at")
        batch.drop_column("publish_requested_by")
