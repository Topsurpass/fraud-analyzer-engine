"""Dismissed flagged rows.

Revision ID: 0007_flag_dismissals
Revises: 0006_connection_ssl
Create Date: 2026-08-24

Flagged rows are recomputed from a query's cached result rather than stored, so
there is no row to mark as reviewed. A dismissal records a sha256 of the row's
values instead, scoped to the query that produced them.

Only the hash is kept. This table cannot be read back into the data it
describes, and a row whose values change no longer matches its dismissal, so it
returns for review -- which is the behaviour a fraud queue needs.

Cascades from saved_queries: dismissals of a deleted query's rows describe rows
that can no longer be produced.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_flag_dismissals"
down_revision = "0006_connection_ssl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flag_dismissals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "query_id",
            sa.String(length=36),
            sa.ForeignKey("saved_queries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "query_id", "row_fingerprint", name="uq_flag_dismissals_row"
        ),
    )
    op.create_index(
        "ix_flag_dismissals_query_id", "flag_dismissals", ["query_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_flag_dismissals_query_id", table_name="flag_dismissals")
    op.drop_table("flag_dismissals")
