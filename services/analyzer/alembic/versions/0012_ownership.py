"""Who owns what.

Revision ID: 0012_ownership
Revises: 0011_users_and_sessions
Create Date: 2026-08-26

Every column is nullable and nothing is backfilled. There is no administrator
at migration time to attribute existing rows to, and inventing one would be
worse than leaving them unowned: an unowned row is visible to administrators
only, which is the safe default, whereas a guessed owner would hand somebody
else's work to whoever happened to be created first.

ON DELETE RESTRICT rather than CASCADE or SET NULL. Accounts are deactivated,
never deleted, and the schema should refuse the deletion rather than quietly
destroy an investigation's queries or orphan them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_ownership"
down_revision = "0011_users_and_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("saved_queries") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_saved_queries_owner", "users", ["owner_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index("ix_saved_queries_owner_id", "saved_queries", ["owner_id"])

    with op.batch_alter_table("dashboards") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_dashboards_owner", "users", ["owner_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index("ix_dashboards_owner_id", "dashboards", ["owner_id"])

    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("created_by", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_connections_created_by", "users", ["created_by"], ["id"], ondelete="RESTRICT"
        )

    with op.batch_alter_table("query_execution_logs") as batch:
        batch.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_query_execution_logs_user", "users", ["user_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index("ix_query_execution_logs_user_id", "query_execution_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_query_execution_logs_user_id", table_name="query_execution_logs")
    with op.batch_alter_table("query_execution_logs") as batch:
        batch.drop_constraint("fk_query_execution_logs_user", type_="foreignkey")
        batch.drop_column("user_id")

    with op.batch_alter_table("connections") as batch:
        batch.drop_constraint("fk_connections_created_by", type_="foreignkey")
        batch.drop_column("created_by")

    op.drop_index("ix_dashboards_owner_id", table_name="dashboards")
    with op.batch_alter_table("dashboards") as batch:
        batch.drop_constraint("fk_dashboards_owner", type_="foreignkey")
        batch.drop_column("owner_id")

    op.drop_index("ix_saved_queries_owner_id", table_name="saved_queries")
    with op.batch_alter_table("saved_queries") as batch:
        batch.drop_constraint("fk_saved_queries_owner", type_="foreignkey")
        batch.drop_column("owner_id")
