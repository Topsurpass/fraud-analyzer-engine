"""Saved-query lifecycle and execution logging."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, DuplicateNameError, ErrorCode, NotFoundError
from app.models import Connection, QueryExecutionLog, SavedQuery
from app.schemas.query import SavedQueryCreate, SavedQueryUpdate
from app.services import query_service, result_cache

logger = logging.getLogger(__name__)


def get_query(session: Session, query_id: str) -> SavedQuery:
    query = session.get(SavedQuery, query_id)
    if query is None:
        raise NotFoundError(
            ErrorCode.QUERY_NOT_FOUND,
            f"No saved query with id {query_id!r}.",
            {"query_id": query_id},
        )
    return query


def list_queries(session: Session, connection_id: str) -> list[SavedQuery]:
    return list(
        session.scalars(
            select(SavedQuery)
            .where(SavedQuery.connection_id == connection_id)
            .order_by(SavedQuery.created_at)
        )
    )


def create_query(
    session: Session, conn: Connection, payload: SavedQueryCreate
) -> SavedQuery:
    """Validate, dry-run, then persist. Nothing is saved if either step fails."""
    row_limit = query_service.resolve_row_limit(payload.row_limit)
    query_service.dry_run(conn, payload.sql_text)

    query = SavedQuery(
        connection_id=conn.id,
        name=payload.name,
        description=payload.description,
        sql_text=payload.sql_text,
        table_hint=payload.table_hint,
        chart_type=payload.chart_type,
        x_field=payload.x_field,
        y_field=payload.y_field,
        series_field=payload.series_field,
        row_limit=row_limit,
        poll_interval_ms=payload.poll_interval_ms,
    )
    session.add(query)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(
            f"A query named {payload.name!r} already exists on this connection.",
            {"name": payload.name},
        ) from exc
    return query


def update_query(
    session: Session, query: SavedQuery, payload: SavedQueryUpdate
) -> SavedQuery:
    """Apply a partial update, re-validating and re-testing any new SQL."""
    data = payload.model_dump(exclude_unset=True)

    if "row_limit" in data and data["row_limit"] is not None:
        data["row_limit"] = query_service.resolve_row_limit(data["row_limit"])
    if data.get("sql_text"):
        query_service.dry_run(query.connection, data["sql_text"])

    for field, value in data.items():
        setattr(query, field, value)

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(
            f"A query named {payload.name!r} already exists on this connection.",
            {"name": payload.name},
        ) from exc

    # The cached result belongs to the old definition. Anything else would
    # serve rows from SQL the user just replaced.
    result_cache.invalidate(query.id)
    return query


def delete_query(session: Session, query: SavedQuery) -> None:
    query_id = query.id
    session.delete(query)
    session.commit()
    result_cache.invalidate(query_id)


def log_execution(
    session: Session,
    query_id: str,
    *,
    success: bool,
    row_count: int | None = None,
    duration_ms: int | None = None,
    error: AppError | None = None,
) -> None:
    """Record an execution attempt.

    Logging must never be the reason a request fails, so a failure to write the
    log is swallowed after being reported.
    """
    entry = QueryExecutionLog(
        query_id=query_id,
        success=success,
        row_count=row_count,
        duration_ms=duration_ms,
        error_code=error.error_code.value if error else None,
        error_message=error.message if error else None,
    )
    try:
        session.add(entry)
        session.commit()
    except Exception:  # noqa: BLE001 - never mask the real result
        logger.exception("Failed to write execution log for query %s", query_id)
        session.rollback()


def recent_logs(session: Session, query_id: str, limit: int = 20) -> list[QueryExecutionLog]:
    return list(
        session.scalars(
            select(QueryExecutionLog)
            .where(QueryExecutionLog.query_id == query_id)
            .order_by(QueryExecutionLog.executed_at.desc())
            .limit(limit)
        )
    )
