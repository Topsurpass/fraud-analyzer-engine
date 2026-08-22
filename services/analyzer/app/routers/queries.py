"""Saved-query CRUD and the execution endpoints a dashboard polls."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.app_state import get_session
from app.errors import AppError
from app.models import utcnow
from app.schemas.query import (
    ExecutionLogRead,
    PollChanged,
    PollUnchanged,
    PreviewRequest,
    PreviewResponse,
    RunResponse,
    SavedQueryCreate,
    SavedQueryRead,
    SavedQueryUpdate,
)
from app.services import connection_service, query_service, result_cache
from app.services import saved_query_service as svc

connection_scoped = APIRouter(prefix="/connections", tags=["queries"])
query_scoped = APIRouter(prefix="/queries", tags=["queries"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@connection_scoped.post(
    "/{connection_id}/queries",
    response_model=SavedQueryRead,
    status_code=status.HTTP_201_CREATED,
)
def create_query(
    connection_id: str,
    payload: SavedQueryCreate,
    session: Session = Depends(get_session),
) -> SavedQueryRead:
    """Save a query.

    The SQL is validated as read-only, then dry-run with a one-row cap against
    this connection. Nothing is persisted unless both succeed, so a saved query
    is always one that actually runs.
    """
    conn = connection_service.get_connection(session, connection_id)
    return SavedQueryRead.model_validate(svc.create_query(session, conn, payload))


@connection_scoped.get("/{connection_id}/queries", response_model=list[SavedQueryRead]) # pyright: ignore[reportIndexIssue]
def list_queries(
    connection_id: str, session: Session = Depends(get_session)
) -> list[SavedQueryRead]:
    """List every saved query on a connection."""
    connection_service.get_connection(session, connection_id)
    return [
        SavedQueryRead.model_validate(q) for q in svc.list_queries(session, connection_id)
    ]


@query_scoped.get("/{query_id}", response_model=SavedQueryRead)
def get_query(query_id: str, session: Session = Depends(get_session)) -> SavedQueryRead:
    return SavedQueryRead.model_validate(svc.get_query(session, query_id))


@query_scoped.put("/{query_id}", response_model=SavedQueryRead)
def update_query(
    query_id: str, payload: SavedQueryUpdate, session: Session = Depends(get_session)
) -> SavedQueryRead:
    """Update a saved query. New SQL is re-validated and re-dry-run."""
    query = svc.get_query(session, query_id)
    return SavedQueryRead.model_validate(svc.update_query(session, query, payload))


@query_scoped.delete("/{query_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_query(query_id: str, session: Session = Depends(get_session)) -> Response:
    svc.delete_query(session, svc.get_query(session, query_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@query_scoped.get("/{query_id}/logs", response_model=list[ExecutionLogRead])
def list_logs(
    query_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[ExecutionLogRead]:
    """Recent execution attempts, newest first. Useful for debugging a chart."""
    svc.get_query(session, query_id)
    return [
        ExecutionLogRead.model_validate(entry)
        for entry in svc.recent_logs(session, query_id, limit)
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _to_run_response(payload, poll_interval_ms: int) -> dict:
    body = asdict(payload)
    body["poll_interval_ms"] = poll_interval_ms
    return body


def _execute_and_log(session: Session, query, conn):
    """Run a saved query, recording the attempt either way."""
    try:
        payload = query_service.run_saved_query(query, conn)
    except AppError as error:
        svc.log_execution(session, query.id, success=False, error=error)
        raise
    svc.log_execution(
        session,
        query.id,
        success=True,
        row_count=payload.row_count,
        duration_ms=payload.duration_ms,
    )
    return payload


@query_scoped.post("/{query_id}/run", response_model=RunResponse)
def run_query(query_id: str, session: Session = Depends(get_session)) -> dict:
    """Execute a saved query now.

    Always hits the target database and refreshes the poll cache, so this is
    the way to force a fresh read.
    """
    query = svc.get_query(session, query_id)
    conn = connection_service.get_connection(session, query.connection_id)

    payload = _execute_and_log(session, query, conn)
    interval = query_service.poll_interval_for(query)
    body = _to_run_response(payload, interval)
    result_cache.set(query.id, payload.data_hash, body, ttl_ms=interval)
    return body


@query_scoped.get("/{query_id}/poll", response_model=PollChanged | PollUnchanged) # pyright: ignore[reportGeneralTypeIssues]
def poll_query(
    query_id: str,
    since_hash: str | None = Query(default=None),
    force: bool = Query(default=False, description="Bypass the cache"),
    session: Session = Depends(get_session),
) -> dict:
    """Cheap endpoint for a dashboard's interval loop.

    Inside the cache TTL this compares hashes in memory and never opens a
    connection to the target database. Without that, a five-second interval
    would run 720 real queries per hour per chart.

    Returns ``changed: false`` when the current hash equals ``since_hash``, and
    the full payload otherwise.
    """
    query = svc.get_query(session, query_id)
    interval = query_service.poll_interval_for(query)

    cached = None if force else result_cache.get(query_id)
    if cached is not None:
        if since_hash and cached.data_hash == since_hash:
            return PollUnchanged(
                query_id=query_id,
                data_hash=cached.data_hash,
                poll_interval_ms=interval,
                from_cache=True,
            ).model_dump()
        return {**cached.payload, "changed": True, "from_cache": True}

    conn = connection_service.get_connection(session, query.connection_id)
    payload = _execute_and_log(session, query, conn)
    body = _to_run_response(payload, interval)
    result_cache.set(query_id, payload.data_hash, body, ttl_ms=interval)

    if since_hash and payload.data_hash == since_hash:
        return PollUnchanged(
            query_id=query_id,
            data_hash=payload.data_hash,
            poll_interval_ms=interval,
            from_cache=False,
        ).model_dump()
    return {**body, "changed": True, "from_cache": False}


@connection_scoped.post("/{connection_id}/query/preview", response_model=PreviewResponse)
def preview_query(
    connection_id: str,
    payload: PreviewRequest,
    session: Session = Depends(get_session),
) -> PreviewResponse:
    """Run ad-hoc SQL without saving it, for a 'try before you save' UX.

    Goes through the same guard as everything else and is capped aggressively,
    since this is exploratory only. Nothing is persisted and nothing is logged.
    """
    conn = connection_service.get_connection(session, connection_id)
    settings = get_settings()
    requested = payload.row_limit or settings.preview_row_limit
    row_limit = min(query_service.resolve_row_limit(requested), settings.preview_row_limit)

    result = query_service.execute_sql(conn, payload.sql_text, row_limit=row_limit)
    return PreviewResponse(
        connection_id=conn.id,
        executed_at=utcnow(),
        duration_ms=result.duration_ms,
        row_count=result.row_count,
        truncated=result.truncated,
        columns=result.columns,
        rows=result.rows,
    )
