"""An append-only record of who changed the system.

Revision ID: 0014_audit_log
Revises: 0013_owner_scoped_names
Create Date: 2026-08-27

Deliberately separate from ``query_execution_logs``: one records who granted
access or changed an account, the other what ran against a customer's
database. A single table trying to answer both questions answers neither well,
and the two are read by different people under different kinds of pressure.

``actor_id`` is RESTRICT rather than SET NULL or CASCADE, matching every other
ownership foreign key added since 0012: accounts are never deleted, so this
should never fire in practice, but an audit entry that has forgotten who
performed it answers nothing, and a hard delete against the database directly
must not be allowed to erase that.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.enums import AuditAction, enum_column

revision = "0014_audit_log"
down_revision = "0013_owner_scoped_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "actor_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", enum_column(AuditAction, length=40), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_target_id", "audit_logs", ["target_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_target_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_table("audit_logs")
