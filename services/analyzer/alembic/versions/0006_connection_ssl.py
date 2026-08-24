"""Per-connection TLS mode for target databases.

Revision ID: 0006_connection_ssl
Revises: 0005_flag_rules
Create Date: 2026-08-24

Target connections carried no TLS setting at all, so every Postgres target ran
on libpq's default of ``prefer``: offer plaintext first, accept it silently if
the server allows it. Two consequences, one loud and one quiet.

The loud one: a managed target that *requires* TLS - Neon, Supabase, RDS with
``rds.force_ssl``, Azure - refuses the connection outright, and the refusal
surfaced as an unrelated "Network is unreachable" because the host also has an
AAAA record and psycopg reports its last attempt first.

The quiet one: every other target could be downgraded to plaintext without
anyone being told.

Existing rows are backfilled to ``require`` rather than to ``prefer``. Backfilling
to today's effective behaviour would preserve the bug for every connection saved
before this migration, which is the opposite of the point; ``require`` is also
what a new connection now gets, so stored rows and new ones behave alike.

``ssl_root_cert`` is nullable and read only by the two verifying modes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.enums import SslMode, enum_column

revision = "0006_connection_ssl"
down_revision = "0005_flag_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table because SQLite is a supported app-state backend and
    # cannot ALTER a column into existence with a NOT NULL constraint.
    with op.batch_alter_table("connections") as batch:
        batch.add_column(
            sa.Column(
                "ssl_mode",
                enum_column(SslMode),
                nullable=False,
                server_default=SslMode.REQUIRE.value,
            )
        )
        batch.add_column(sa.Column("ssl_root_cert", sa.Text(), nullable=True))

    # The server default covers rows added from here on. This covers the rows
    # that were already there when the column appeared: on some backends
    # add_column fills existing rows with NULL regardless of the default.
    connections = sa.table(
        "connections",
        sa.column("ssl_mode", sa.String),
    )
    op.execute(
        connections.update()
        .where(connections.c.ssl_mode.is_(None))
        .values(ssl_mode=SslMode.REQUIRE.value)
    )


def downgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("ssl_root_cert")
        batch.drop_column("ssl_mode")
