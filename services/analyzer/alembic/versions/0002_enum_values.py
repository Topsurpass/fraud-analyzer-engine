"""Store enum values instead of names

SQLAlchemy persisted these columns by enum member name ("SQLITE", "LINE"),
while every server_default was written as a value ("untested", "table"). Any
row created outside the ORM was therefore unreadable by it, raising
LookupError, and a client reading the database directly saw different strings
than the API returns for the same field.

This rewrites existing rows to the value spelling. It is idempotent: rows
already holding lowercase values are left alone by the CASE expressions.

Revision ID: 0002_enum_values
Revises: 6d3d257eda35
"""

from __future__ import annotations

from alembic import op

revision = "0002_enum_values"
down_revision = "6d3d257eda35"
branch_labels = None
depends_on = None

#: column -> {stored name: intended value}
_NAME_TO_VALUE = {
    ("connections", "db_type"): {
        "POSTGRES": "postgres",
        "MYSQL": "mysql",
        "SQLITE": "sqlite",
    },
    ("connections", "status"): {
        "UNTESTED": "untested",
        "OK": "ok",
        "FAILED": "failed",
    },
    ("saved_queries", "chart_type"): {
        "LINE": "line",
        "BAR": "bar",
        "PIE": "pie",
        "NUMBER": "number",
        "TABLE": "table",
    },
}


def _remap(mapping: dict[str, str], reverse: bool = False) -> None:
    for (table, column), pairs in _NAME_TO_VALUE.items():
        for name, value in pairs.items():
            src, dst = (value, name) if reverse else (name, value)
            op.execute(
                f"UPDATE {table} SET {column} = '{dst}' WHERE {column} = '{src}'"
            )


def upgrade() -> None:
    _remap(_NAME_TO_VALUE)


def downgrade() -> None:
    _remap(_NAME_TO_VALUE, reverse=True)
