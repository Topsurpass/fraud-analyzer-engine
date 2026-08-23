"""Composite index for the execution-log query pattern.

Revision ID: 0004_log_index
Revises: 0003_dashboards
Create Date: 2026-08-23

``recent_logs`` filters on ``query_id`` and orders by ``executed_at DESC``
with a LIMIT. The separate single-column indexes could not serve that: the
planner scanned the query_id index and then sorted the entire match set. With
retention now bounded but a busy card still accumulating thousands of rows
inside the window, every /logs call paid that sort.

A composite index on (query_id, executed_at DESC) answers the whole thing from
the index, in order, and stops at the LIMIT.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_log_index"
down_revision = "0003_dashboards"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_logs_query_executed"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "query_execution_logs",
        ["query_id", sa.text("executed_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="query_execution_logs")
