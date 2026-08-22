"""Schema discovery for a target database.

Everything here goes through ``sqlalchemy.inspect``, so it works against any
schema without the engine knowing a single table or column name in advance.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.db import target_registry
from app.errors import AppError, ErrorCode, NotFoundError
from app.models import Connection
from app.schemas.connection import ColumnInfo, TableInfo


def list_tables(conn: Connection) -> list[TableInfo]:
    try:
        with target_registry.read_only_connection(conn) as sa_conn:
            inspector = inspect(sa_conn)
            tables = [TableInfo(name=n, kind="table") for n in inspector.get_table_names()]
            views = [TableInfo(name=n, kind="view") for n in inspector.get_view_names()]
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise target_registry.translate_db_error(exc) from exc

    return sorted(tables + views, key=lambda t: (t.kind, t.name))


def list_columns(conn: Connection, table_name: str) -> list[ColumnInfo]:
    try:
        with target_registry.read_only_connection(conn) as sa_conn:
            inspector = inspect(sa_conn)
            known = set(inspector.get_table_names()) | set(inspector.get_view_names())
            if table_name not in known:
                raise NotFoundError(
                    ErrorCode.TABLE_NOT_FOUND,
                    f"No table or view named {table_name!r} on this connection.",
                    {"table": table_name},
                )
            raw_columns = inspector.get_columns(table_name)
            try:
                primary_keys = set(
                    inspector.get_pk_constraint(table_name).get("constrained_columns") or []
                )
            except Exception:  # noqa: BLE001 - views have no primary key
                primary_keys = set()
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise target_registry.translate_db_error(exc) from exc

    return [
        ColumnInfo(
            name=col["name"],
            type=str(col["type"]),
            nullable=bool(col.get("nullable", True)),
            primary_key=col["name"] in primary_keys,
        )
        for col in raw_columns
    ]
