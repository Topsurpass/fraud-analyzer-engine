"""Read-only schema discovery, so a UI can build queries against any schema."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.schemas.connection import ColumnList, TableList
from app.services import connection_service, introspection_service

router = APIRouter(prefix="/connections", tags=["introspection"])


@router.get("/{connection_id}/tables", response_model=TableList)
def list_tables(connection_id: str, session: Session = Depends(get_session)) -> TableList:
    """List every table and view visible to this connection's credentials."""
    conn = connection_service.get_connection(session, connection_id)
    return TableList(
        connection_id=conn.id, tables=introspection_service.list_tables(conn)
    )


@router.get("/{connection_id}/tables/{table_name}/columns", response_model=ColumnList)
def list_columns(
    connection_id: str, table_name: str, session: Session = Depends(get_session)
) -> ColumnList:
    """List the columns and types of one table or view."""
    conn = connection_service.get_connection(session, connection_id)
    return ColumnList(
        connection_id=conn.id,
        table=table_name,
        columns=introspection_service.list_columns(conn, table_name),
    )
