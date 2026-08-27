"""Names are unique per owner, and an ad-hoc run is still attributable.

Revision ID: 0013_owner_scoped_names
Revises: 0012_ownership
Create Date: 2026-08-27

Two changes, both consequences of the ownership model landing in 0012.

**Owner-scoped names.** ``uq_dashboards_name`` was unique on ``name`` alone and
``uq_saved_queries_conn_name`` on ``(connection_id, name)`` while connections
are shared by every analyst. Either one turned a name into a global namespace:
the second analyst to reach for "Fraud Review" got a 409 describing a resource
that is absent from their listing and that they may not read - the same
cross-analyst existence leak the 404-not-403 rule closes on the read path,
arriving through the write path instead. Both become owner-scoped here.

Widening a unique key can never fail on existing data: every row that satisfied
the old constraint satisfies the new one. NULL owners are the one nuance - SQL
treats NULLs in a unique constraint as distinct, so unowned rows predating
accounts stop colliding with each other. Every create sets an owner, so that
reaches nothing new, and ``fae create-admin`` offers to claim the unowned ones.

**A preview has no saved query.** ``POST /connections/{id}/query/preview`` runs
analyst-authored SQL against a customer database, and the design's measurable
outcome is an execution-log row carrying a ``user_id`` for 100% of runs. There
is no ``saved_queries`` row to hang that on, so ``query_id`` becomes nullable
and ``connection_id`` is added: a log row that cannot name the database it ran
against does not answer the question the log exists for. Both are nullable,
and exactly one of them is set on any given row.

Everything runs through ``op.batch_alter_table``: SQLite cannot ALTER a
constraint in place, so Alembic recreates each table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_owner_scoped_names"
down_revision = "0012_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dashboards") as batch:
        batch.drop_constraint("uq_dashboards_name", type_="unique")
        batch.create_unique_constraint("uq_dashboards_owner_name", ["owner_id", "name"])

    with op.batch_alter_table("saved_queries") as batch:
        batch.drop_constraint("uq_saved_queries_conn_name", type_="unique")
        batch.create_unique_constraint(
            "uq_saved_queries_conn_owner_name", ["connection_id", "owner_id", "name"]
        )

    with op.batch_alter_table("query_execution_logs") as batch:
        batch.alter_column(
            "query_id", existing_type=sa.String(length=36), nullable=True
        )
        batch.add_column(sa.Column("connection_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_query_execution_logs_connection",
            "connections",
            ["connection_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "ix_query_execution_logs_connection_id", "query_execution_logs", ["connection_id"]
    )


def downgrade() -> None:
    # Rows a preview wrote have no query_id, and the pre-0013 schema has no
    # column that can hold them. Dropped rather than left to fail the NOT NULL:
    # the alternative is a downgrade that cannot run at all on any database
    # where a preview has been used once.
    op.execute("DELETE FROM query_execution_logs WHERE query_id IS NULL")
    op.drop_index(
        "ix_query_execution_logs_connection_id", table_name="query_execution_logs"
    )
    with op.batch_alter_table("query_execution_logs") as batch:
        batch.drop_constraint("fk_query_execution_logs_connection", type_="foreignkey")
        batch.drop_column("connection_id")
        batch.alter_column(
            "query_id", existing_type=sa.String(length=36), nullable=False
        )

    with op.batch_alter_table("saved_queries") as batch:
        batch.drop_constraint("uq_saved_queries_conn_owner_name", type_="unique")
        batch.create_unique_constraint(
            "uq_saved_queries_conn_name", ["connection_id", "name"]
        )

    with op.batch_alter_table("dashboards") as batch:
        batch.drop_constraint("uq_dashboards_owner_name", type_="unique")
        batch.create_unique_constraint("uq_dashboards_name", ["name"])
