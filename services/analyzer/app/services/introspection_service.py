"""Schema discovery for a target database.

Everything here goes through ``sqlalchemy.inspect``, so it works against any
schema without the engine knowing a single table or column name in advance.

No error translation happens in this module: ``read_only_connection`` already
maps every driver exception onto the API's taxonomy and passes ``AppError``
through unchanged, so a second layer here would be dead code.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.db import target_registry
from app.errors import ErrorCode, NotFoundError
from app.models import Connection
from app.schemas.connection import ColumnInfo, TableInfo


def list_tables(conn: Connection) -> list[TableInfo]:
    """Every table and view the connection's credentials can see."""
    with target_registry.read_only_connection(conn) as sa_conn:
        inspector = inspect(sa_conn)
        tables = [TableInfo(name=n, kind="table") for n in inspector.get_table_names()]
        views = [TableInfo(name=n, kind="view") for n in inspector.get_view_names()]

    return sorted(tables + views, key=lambda t: (t.kind, t.name))


def list_columns(conn: Connection, table_name: str) -> list[ColumnInfo]:
    """Columns and types for one table or view.

    Asks for the columns first and treats "no such table" as the 404, rather
    than listing every table and view up front to validate the name. On a
    warehouse with thousands of tables that listing is a catalog scan, and it
    ran on every column expansion in the schema browser -- three round trips to
    answer a question that takes one. The listing is still consulted, but only
    on the failure path, where its cost buys a precise error.
    """
    with target_registry.read_only_connection(conn) as sa_conn:
        inspector = inspect(sa_conn)
        try:
            raw_columns = inspector.get_columns(table_name)
        except Exception as exc:  # noqa: BLE001 - re-raised below as a 404
            raise NotFoundError(
                ErrorCode.TABLE_NOT_FOUND,
                f"No table or view named {table_name!r} on this connection.",
                {"table": table_name},
            ) from exc

        # SQLite returns an empty list rather than raising for an unknown
        # table, so emptiness still has to be disambiguated: a real table with
        # no columns cannot exist, but a view might resolve oddly, so the
        # authoritative listing settles it.
        if not raw_columns:
            known = set(inspector.get_table_names()) | set(inspector.get_view_names())
            if table_name not in known:
                raise NotFoundError(
                    ErrorCode.TABLE_NOT_FOUND,
                    f"No table or view named {table_name!r} on this connection.",
                    {"table": table_name},
                )

        try:
            constraint = inspector.get_pk_constraint(table_name)
            primary_keys = set(constraint.get("constrained_columns") or [])
        except Exception:  # pragma: no cover - some backends raise on views
            primary_keys = set()

    return [
        ColumnInfo(
            name=col["name"],
            type=str(col["type"]),
            nullable=bool(col.get("nullable", True)),
            primary_key=col["name"] in primary_keys,
        )
        for col in raw_columns
    ]
