"""Flag rules and their conditions.

Revision ID: 0005_flag_rules
Revises: 0004_log_index
Create Date: 2026-08-24

Lets an analyst say what "flagged" means for their own data instead of relying
on the frontend guessing from column names. A rule belongs to one saved query,
ANDs its conditions, and a row is flagged when any enabled rule matches it.

Conditions are a table rather than a JSON column so the enum constraint is real
and a condition cannot outlive its rule.

Both enum columns are VARCHAR via ``enum_column``, matching every other enum in
this schema: they store the value ("high"), not the member name ("HIGH"), so a
row written by plain SQL is still readable by the ORM.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.enums import FlagOperator, FlagSeverity, enum_column

revision = "0005_flag_rules"
down_revision = "0004_log_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flag_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "query_id",
            sa.String(length=36),
            sa.ForeignKey("saved_queries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "severity",
            enum_column(FlagSeverity),
            nullable=False,
            server_default=FlagSeverity.MEDIUM.value,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("query_id", "name", name="uq_flag_rules_query_name"),
    )
    op.create_index("ix_flag_rules_query_id", "flag_rules", ["query_id"])

    op.create_table(
        "flag_conditions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "rule_id",
            sa.String(length=36),
            sa.ForeignKey("flag_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("operator", enum_column(FlagOperator), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value2", sa.Text(), nullable=True),
    )
    op.create_index("ix_flag_conditions_rule_id", "flag_conditions", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_flag_conditions_rule_id", table_name="flag_conditions")
    op.drop_table("flag_conditions")
    op.drop_index("ix_flag_rules_query_id", table_name="flag_rules")
    op.drop_table("flag_rules")
