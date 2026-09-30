"""Named lists, and flag conditions that compare against them.

Revision ID: 0016_lists
Revises: 0015_chart_publishing
Create Date: 2026-09-30

A watchlist of a few hundred account numbers or blocked terminals used to be
pasted, comma separated, into every rule that needed it. Lists are stored once,
named and described, and a condition points at one with ``list_id``.

``flag_conditions.list_id`` is RESTRICT: the database itself refuses to delete
a list a rule still reads, so a delete that races a rule save cannot leave a
condition pointing at nothing. The service checks first to give a friendly 409.

``item_lists.created_by`` is SET NULL, the opposite choice from
``published_by`` in 0015: a list should outlive its creator (an unowned list is
then editable by admins only), whereas a publication must remember who made it.

Names are unique through ``name_key`` (NFC, case-folded, computed by the
service), not a ``lower(name)`` index: SQLite's ``lower()`` is ASCII-only, so
"Eclair" and "eclair" with accents would be two names there and one on Postgres.

``version`` counts saves; the member-set cache keys on it.

No data is backfilled: existing ``in`` / ``not_in`` rules keep their typed values.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_lists"
down_revision = "0015_chart_publishing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_lists",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=600), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_item_lists_created_by", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_item_lists_name_key", "item_lists", ["name_key"], unique=True)

    op.create_table(
        "list_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["list_id"], ["item_lists.id"], name="fk_list_items_list_id", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_list_items_list_id", "list_items", ["list_id"])

    with op.batch_alter_table("flag_conditions") as batch:
        batch.add_column(sa.Column("list_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_flag_conditions_list_id",
            "item_lists",
            ["list_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_flag_conditions_list_id", ["list_id"])


def downgrade() -> None:
    with op.batch_alter_table("flag_conditions") as batch:
        batch.drop_index("ix_flag_conditions_list_id")
        batch.drop_constraint("fk_flag_conditions_list_id", type_="foreignkey")
        batch.drop_column("list_id")

    op.drop_index("ix_list_items_list_id", table_name="list_items")
    op.drop_table("list_items")
    op.drop_index("ix_item_lists_name_key", table_name="item_lists")
    op.drop_table("item_lists")
