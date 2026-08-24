"""Persist the rows a query's flag rules matched.

Revision ID: 0008_flagged_rows
Revises: 0007_flag_dismissals
Create Date: 2026-08-24

Flagged rows were recomputed from the result cache, so they existed only while
a cache entry did and only if somebody had recently opened the query. Storing
them makes the flagged view a real queue: findings accumulate while nobody is
watching, survive a restart, and carry when they were first seen.

The engine now holds a copy of matched rows' values. Bounded to rows that
matched a rule someone wrote, cascading away with the query, and deleted when
the row is dismissed. Nothing here is ever written to a target database; those
connections are opened read-only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.enums import FlagSeverity, enum_column

revision = "0008_flagged_rows"
down_revision = "0007_flag_dismissals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flagged_rows",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "query_id",
            sa.String(length=36),
            sa.ForeignKey("saved_queries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_fingerprint", sa.String(length=64), nullable=False),
        # JSON rather than columns: the engine never knows the target's schema,
        # and every query returns a different shape.
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("rule_ids", sa.JSON(), nullable=False),
        sa.Column("rule_names", sa.JSON(), nullable=False),
        sa.Column("severity", enum_column(FlagSeverity), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("query_id", "row_fingerprint", name="uq_flagged_rows_row"),
    )
    op.create_index("ix_flagged_rows_query_id", "flagged_rows", ["query_id"])
    op.create_index(
        "ix_flagged_rows_query_seen", "flagged_rows", ["query_id", "first_seen_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_flagged_rows_query_seen", table_name="flagged_rows")
    op.drop_index("ix_flagged_rows_query_id", table_name="flagged_rows")
    op.drop_table("flagged_rows")
