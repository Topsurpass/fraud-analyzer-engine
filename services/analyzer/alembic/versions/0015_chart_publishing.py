"""Charts an analyst can share with the whole team.

Revision ID: 0015_chart_publishing
Revises: 0014_audit_log
Create Date: 2026-08-28

Private-by-default work needs a way out, or the only sharing mechanism is
sending somebody a screenshot. Publishing is that way out, and it is
self-service so it actually gets used: an analyst publishes a chart they own,
an admin publishes anyone's.

``published_by`` is not decoration. It decides who may *un*publish: an analyst
can retract their own publication, while a chart an admin published stays the
admin's to retract. That asymmetry is what gives an admin a real freeze over
another person's work rather than one the author can undo.

Nothing is backfilled. Every existing chart starts private, which is the only
safe default: publishing on migration would expose one analyst's work to the
whole team without anybody choosing to.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_chart_publishing"
down_revision = "0014_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("query_charts") as batch:
        # sa.false(), never text("0"): SQLite accepts an integer default for a
        # boolean and Postgres rejects it outright, so the integer form passes
        # the whole test suite and then refuses to boot in production.
        batch.add_column(
            sa.Column(
                "is_public", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("published_by", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
        )
        # RESTRICT rather than SET NULL. Accounts are never deleted, and a
        # publication that has forgotten who made it cannot answer the one
        # question the column exists for: who is allowed to retract this.
        batch.create_foreign_key(
            "fk_query_charts_published_by",
            "users",
            ["published_by"],
            ["id"],
            ondelete="RESTRICT",
        )

    # Every published-chart lookup filters on this, and it is the query behind
    # the shared board every signed-in user loads.
    op.create_index("ix_query_charts_is_public", "query_charts", ["is_public"])


def downgrade() -> None:
    op.drop_index("ix_query_charts_is_public", table_name="query_charts")
    with op.batch_alter_table("query_charts") as batch:
        batch.drop_constraint("fk_query_charts_published_by", type_="foreignkey")
        batch.drop_column("published_at")
        batch.drop_column("published_by")
        batch.drop_column("is_public")
