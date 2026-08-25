"""Several charts per query, and a connection you can pause.

Revision ID: 0009_query_charts
Revises: 0008_flagged_rows
Create Date: 2026-08-25

Chart configuration lived on ``saved_queries``, which made "a query" and "a
chart" the same object. Three views of one result meant three saved queries -
and because the result cache is keyed by query id, three executions of
identical SQL against the customer's database on every poll. The SQL now runs
once and each chart is a mapping onto the columns it already returned.

Nothing is lost in the move: every existing query is backfilled to exactly one
chart carrying its current configuration, and every dashboard item is
repointed at that chart. A board looks identical after this migration; it just
places a chart instead of a query.

The ``paused`` flag on connections is deliberately separate from ``status``.
Status records how the last *test* went, and folding "I turned this off" into
it would destroy the answer to "was it working when I paused it".
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

from app.models.enums import ChartType, enum_column

revision = "0009_query_charts"
down_revision = "0008_flagged_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    op.create_table(
        "query_charts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "query_id",
            sa.String(length=36),
            sa.ForeignKey("saved_queries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "chart_type",
            enum_column(ChartType),
            nullable=False,
            server_default=ChartType.TABLE.value,
        ),
        sa.Column("x_field", sa.String(length=255), nullable=True),
        sa.Column("y_field", sa.String(length=255), nullable=True),
        sa.Column("series_field", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("query_id", "name", name="uq_query_charts_query_name"),
    )
    op.create_index("ix_query_charts_query_id", "query_charts", ["query_id"])

    # --- backfill one chart per existing query -----------------------------
    #
    # Named after the query itself. A single-chart query reads the same as it
    # always did, and the name only becomes load-bearing once a second chart
    # is added beside it.
    existing = connection.execute(
        sa.text(
            "SELECT id, name, chart_type, x_field, y_field, series_field, "
            "created_at, updated_at FROM saved_queries"
        )
    ).fetchall()

    chart_for_query: dict[str, str] = {}
    for row in existing:
        chart_id = str(uuid.uuid4())
        chart_for_query[row.id] = chart_id
        connection.execute(
            sa.text(
                "INSERT INTO query_charts (id, query_id, name, position, "
                "chart_type, x_field, y_field, series_field, created_at, "
                "updated_at) VALUES (:id, :query_id, :name, 0, :chart_type, "
                ":x_field, :y_field, :series_field, :created_at, :updated_at)"
            ),
            {
                "id": chart_id,
                "query_id": row.id,
                # Truncated to the column width: a query name is the same 200
                # characters, so this can only bite if one is at the limit.
                "name": (row.name or "Chart")[:200],
                "chart_type": row.chart_type or ChartType.TABLE.value,
                "x_field": row.x_field,
                "y_field": row.y_field,
                "series_field": row.series_field,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            },
        )

    # --- repoint dashboard items at charts ---------------------------------
    #
    # Rebuilt rather than altered: the primary key changes from
    # (dashboard_id, query_id) to (dashboard_id, chart_id), which SQLite cannot
    # do in place and which is clearer to read as a rebuild on any backend.
    items = connection.execute(
        sa.text("SELECT dashboard_id, query_id, position FROM dashboard_items")
    ).fetchall()

    op.drop_table("dashboard_items")
    op.create_table(
        "dashboard_items",
        sa.Column(
            "dashboard_id",
            sa.String(length=36),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "chart_id",
            sa.String(length=36),
            sa.ForeignKey("query_charts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
    )

    for item in items:
        chart_id = chart_for_query.get(item.query_id)
        if chart_id is None:  # pragma: no cover - FK made this impossible
            continue
        connection.execute(
            sa.text(
                "INSERT INTO dashboard_items (dashboard_id, chart_id, position) "
                "VALUES (:dashboard_id, :chart_id, :position)"
            ),
            {
                "dashboard_id": item.dashboard_id,
                "chart_id": chart_id,
                "position": item.position,
            },
        )

    # --- the query keeps only what it is: SQL, limits, cadence -------------
    with op.batch_alter_table("saved_queries") as batch:
        batch.drop_column("series_field")
        batch.drop_column("y_field")
        batch.drop_column("x_field")
        batch.drop_column("chart_type")

    with op.batch_alter_table("connections") as batch:
        batch.add_column(
            # sa.false(), not text("0"): SQLite accepts an integer default for
            # a boolean and Postgres rejects it outright with a datatype
            # mismatch. The test suite runs on SQLite, so only a real Postgres
            # start surfaced this - which it did, by refusing to boot.
            sa.Column(
                "paused", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    connection = op.get_bind()

    with op.batch_alter_table("connections") as batch:
        batch.drop_column("paused")

    with op.batch_alter_table("saved_queries") as batch:
        batch.add_column(
            sa.Column(
                "chart_type",
                enum_column(ChartType),
                nullable=False,
                server_default=ChartType.TABLE.value,
            )
        )
        batch.add_column(sa.Column("x_field", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("y_field", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("series_field", sa.String(length=255), nullable=True))

    # The first chart wins. A query with several charts cannot round-trip into
    # a model that holds one, and picking the first is the only choice that
    # leaves the board it came from looking the same.
    charts = connection.execute(
        sa.text(
            "SELECT query_id, id, chart_type, x_field, y_field, series_field "
            "FROM query_charts ORDER BY position"
        )
    ).fetchall()

    seen: dict[str, str] = {}
    for chart in charts:
        if chart.query_id in seen:
            continue
        seen[chart.query_id] = chart.id
        connection.execute(
            sa.text(
                "UPDATE saved_queries SET chart_type = :chart_type, "
                "x_field = :x_field, y_field = :y_field, "
                "series_field = :series_field WHERE id = :id"
            ),
            {
                "chart_type": chart.chart_type,
                "x_field": chart.x_field,
                "y_field": chart.y_field,
                "series_field": chart.series_field,
                "id": chart.query_id,
            },
        )

    query_for_chart = {
        chart.id: chart.query_id for chart in charts if seen.get(chart.query_id) == chart.id
    }
    items = connection.execute(
        sa.text("SELECT dashboard_id, chart_id, position FROM dashboard_items")
    ).fetchall()

    op.drop_table("dashboard_items")
    op.create_table(
        "dashboard_items",
        sa.Column(
            "dashboard_id",
            sa.String(length=36),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "query_id",
            sa.String(length=36),
            sa.ForeignKey("saved_queries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
    )

    placed: set[tuple[str, str]] = set()
    for item in items:
        query_id = query_for_chart.get(item.chart_id)
        # A board placing a second chart of the same query collapses onto one
        # row here; the composite key cannot hold it twice.
        if query_id is None or (item.dashboard_id, query_id) in placed:
            continue
        placed.add((item.dashboard_id, query_id))
        connection.execute(
            sa.text(
                "INSERT INTO dashboard_items (dashboard_id, query_id, position) "
                "VALUES (:dashboard_id, :query_id, :position)"
            ),
            {
                "dashboard_id": item.dashboard_id,
                "query_id": query_id,
                "position": item.position,
            },
        )

    op.drop_index("ix_query_charts_query_id", table_name="query_charts")
    op.drop_table("query_charts")
