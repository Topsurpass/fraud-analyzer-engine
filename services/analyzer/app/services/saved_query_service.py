"""Saved-query lifecycle and execution logging."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, DuplicateNameError, ErrorCode, NotFoundError
from app.models import Connection, QueryExecutionLog, SavedQuery, utcnow
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


def list_queries_by_ids(session: Session, query_ids: list[str]) -> list[SavedQuery]:
    """Fetch many saved queries in one statement, in the order asked for.

    A dashboard card resolves to a saved query, and a board may span several
    connections, so the per-connection listing cannot serve one. Without this
    the frontend issues one GET per card: a twelve-card board was thirteen
    round trips before first paint, each with its own app-state session.

    Unknown ids are skipped rather than raising, because a board that has just
    lost a query should still render the cards that survived.
    """
    if not query_ids:
        return []

    found = {
        query.id: query
        for query in session.scalars(
            select(SavedQuery).where(SavedQuery.id.in_(query_ids))
        )
    }
    return [found[qid] for qid in query_ids if qid in found]


def prune_execution_logs(session: Session | None = None) -> int:
    """Delete execution logs past the retention window. Returns rows removed.

    Two limits, because they fail differently. ``log_retention_days`` bounds
    age, which is what stops the table growing forever on a metered backend.
    ``max_logs_per_query`` bounds depth per query, which is what stops one
    busy card from burying every other query's history inside the window.

    Opens its own session when not given one, so startup can call it before
    any request has created a session.
    """
    settings = get_settings()
    if settings.log_retention_days <= 0 and settings.max_logs_per_query <= 0:
        return 0

    owns_session = session is None
    if session is None:
        from app.db.app_state import get_sessionmaker

        session = get_sessionmaker()()

    try:
        removed = 0

        if settings.log_retention_days > 0:
            cutoff = utcnow() - timedelta(days=settings.log_retention_days)
            result = session.execute(
                delete(QueryExecutionLog).where(QueryExecutionLog.executed_at < cutoff)
            )
            removed += result.rowcount or 0

        if settings.max_logs_per_query > 0:
            removed += _trim_per_query(session, settings.max_logs_per_query)

        session.commit()
        return removed
    finally:
        if owns_session:
            session.close()


def _trim_per_query(session: Session, keep: int) -> int:
    """Keep only the newest ``keep`` rows per query.

    Done as one grouped scan plus a delete per over-quota query rather than a
    window function, because the app-state backend may be SQLite or Postgres
    and this keeps one code path for both. Queries at or under quota cost
    nothing beyond the initial count.
    """
    over_quota = session.execute(
        select(QueryExecutionLog.query_id)
        .group_by(QueryExecutionLog.query_id)
        .having(func.count(QueryExecutionLog.id) > keep)
    ).scalars()

    removed = 0
    for query_id in list(over_quota):
        cutoff_row = session.execute(
            select(QueryExecutionLog.executed_at)
            .where(QueryExecutionLog.query_id == query_id)
            .order_by(QueryExecutionLog.executed_at.desc())
            .offset(keep)
            .limit(1)
        ).scalar_one_or_none()
        if cutoff_row is None:
            continue
        result = session.execute(
            delete(QueryExecutionLog).where(
                QueryExecutionLog.query_id == query_id,
                QueryExecutionLog.executed_at <= cutoff_row,
            )
        )
        removed += result.rowcount or 0
    return removed
