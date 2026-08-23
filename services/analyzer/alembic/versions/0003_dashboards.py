"""Dashboards and their ordered saved-query items

A dashboard groups saved queries into one view. Membership lives in an
association table rather than a JSON column of ids so the database keeps the
reference honest: deleting a saved query removes it from every dashboard
through the foreign key, instead of leaving boards pointing at rows that no
longer exist.

Revision ID: 0003_dashboards
Revises: 0002_enum_values
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_dashboards"
down_revision = "0002_enum_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_dashboards_name"),
    )

    op.create_table(
        "dashboard_items",
        sa.Column("dashboard_id", sa.String(length=36), nullable=False),
        sa.Column("query_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["query_id"], ["saved_queries.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("dashboard_id", "query_id"),
    )


def downgrade() -> None:
    op.drop_table("dashboard_items")
    op.drop_table("dashboards")
