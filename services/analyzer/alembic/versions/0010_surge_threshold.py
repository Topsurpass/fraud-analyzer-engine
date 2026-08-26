"""A configurable surge threshold per chart.

Revision ID: 0010_surge_threshold
Revises: 0009_query_charts
Create Date: 2026-08-26

Terminals do not carry comparable volume: one does twenty times another's, so
an absolute jump means nothing across a fleet. A *percentage* change is
volume-independent, which is why the threshold is expressed as one - the same
number is meaningful for the busiest terminal and the quietest.

Nullable rather than defaulted, so "unset" and "set to the default value" stay
distinguishable. A chart with NULL uses the app-wide default, and changing that
default later moves every unset chart with it; a chart carrying a number keeps
it. Backfilling a value here would have thrown that distinction away
permanently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_surge_threshold"
down_revision = "0009_query_charts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("query_charts") as batch:
        batch.add_column(sa.Column("surge_threshold_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("query_charts") as batch:
        batch.drop_column("surge_threshold_pct")
