"""Saved-query lifecycle and execution logging."""

from __future__ import annotations

import logging
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.errors import AppError, DuplicateNameError, ErrorCode, NotFoundError
from app.models import (
    ChartType,
    Connection,
    QueryChart,
    QueryExecutionLog,
    SavedQuery,
    utcnow,
)
from app.models.user import User
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


def visible_to(user: User):
    """The filter clause deciding which queries a caller may see.

    Returns a clause rather than a query so every call site composes it into
    whatever it was already selecting, instead of each one re-deriving the
    rule.

    An administrator sees everything, including unowned rows. An analyst sees
    only rows they own - never unowned ones, which belong to the era before
    accounts and must not fall to whoever signs in first.
    """
    if user.is_admin:
        return sa.true()
    return SavedQuery.owner_id == user.id


def get_owned(session: Session, query_id: str, user: User) -> SavedQuery:
    """One query the caller is entitled to, or raise QUERY_NOT_FOUND.

    Not found rather than forbidden, deliberately. Confirming that an id
    exists but belongs to somebody else tells an analyst what a colleague is
    working on, and the id is guessable from any URL that has been pasted
    into a chat.
    """
    query = session.get(SavedQuery, query_id)
    if query is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such query.")
    if not user.is_admin and query.owner_id != user.id:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such query.")
    return query


def list_queries(session: Session, connection_id: str, user: User) -> list[SavedQuery]:
    """Every saved query on a connection that the caller may see, with its
    charts already loaded.

    ``selectinload`` is load-bearing: the read model includes each query's
    charts, and a connection with twenty queries would otherwise lazy-load
    twenty chart sets one statement at a time.

    Filtered by ``visible_to`` even though this is scoped to one connection
    already: a connection is shared across every analyst who queries it, but
    the queries saved against it are not.
    """
    return list(
        session.scalars(
            select(SavedQuery)
            .where(SavedQuery.connection_id == connection_id, visible_to(user))
            .options(selectinload(SavedQuery.charts))
            .order_by(SavedQuery.created_at)
        )
    )


def list_visible_queries(session: Session, user: User) -> list[SavedQuery]:
    """Every query the caller may see, across every connection.

    The "what have I got" view behind ``GET /queries`` with no ``ids``
    filter, as opposed to :func:`list_queries_by_ids`, which resolves a
    specific set of cards a dashboard already knows about. Both delegate to
    :func:`visible_to` so the two call sites can never drift apart on who
    gets to see what.
    """
    return list(
        session.scalars(
            select(SavedQuery)
            .where(visible_to(user))
            .options(selectinload(SavedQuery.charts))
            .order_by(SavedQuery.created_at)
        )
    )


def create_query(
    session: Session,
    conn: Connection,
    payload: SavedQueryCreate,
    owner_id: str | None = None,
) -> SavedQuery:
    """Validate, dry-run, then persist. Nothing is saved if either step fails.

    ``owner_id`` defaults to ``None`` rather than being required, so internal
    or scripted callers that have no caller to attribute the query to still
    work - the row just comes out unowned, visible to administrators only.
    """
    row_limit = query_service.resolve_row_limit(payload.row_limit)
    query_service.dry_run(conn, payload.sql_text)

    query = SavedQuery(
        connection_id=conn.id,
        name=payload.name,
        description=payload.description,
        sql_text=payload.sql_text,
        table_hint=payload.table_hint,
        row_limit=row_limit,
        poll_interval_ms=payload.poll_interval_ms,
        owner_id=owner_id,
    )
    # A query with no chart renders nothing, and a person who just wrote some
    # SQL has not asked to configure a chart yet. One table chart is the
    # honest default: it shows the rows exactly as returned, and it is what
    # every query had before charts became separable.
    query.charts.append(
        QueryChart(name=payload.name[:200], position=0, chart_type=ChartType.TABLE)
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
    query_id: str | None,
    *,
    success: bool,
    row_count: int | None = None,
    duration_ms: int | None = None,
    error: AppError | None = None,
    user_id: str | None = None,
    connection_id: str | None = None,
) -> None:
    """Record an execution attempt.

    ``user_id`` answers "who caused this run", the audit question this feature
    exists for, and the design measures itself on execution-log rows carrying
    one for 100% of runs. Exactly one caller is entitled to leave it None:
    ``app/services/scheduler.py``, which runs on a timer with nobody behind
    it. Every other path is somebody asking, including the ones that finish
    after the response has been sent - the stale-cache refresher in
    ``app/services/refresher.py`` runs behind a named analyst's poll and
    carries that analyst's id, and the flagged refresh in
    ``app/services/flag_rule_service.py`` fans one click out into several
    executions that all belong to the person who clicked. A row with no user
    means the scheduler, and nothing else.

    ``query_id`` is None for an ad-hoc preview, which has no saved query;
    ``connection_id`` names the database in that case, and is left None for a
    saved query, which already names its own connection.

    Logging must never be the reason a request fails, so a failure to write the
    log is swallowed after being reported.
    """
    entry = QueryExecutionLog(
        query_id=query_id,
        connection_id=connection_id,
        success=success,
        row_count=row_count,
        duration_ms=duration_ms,
        error_code=error.error_code.value if error else None,
        error_message=error.message if error else None,
        user_id=user_id,
    )
    try:
        session.add(entry)
        session.commit()
    except Exception:  # noqa: BLE001 - never mask the real result
        logger.exception(
            "Failed to write execution log for query %s", query_id or "(preview)"
        )
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


def list_queries_by_ids(
    session: Session, query_ids: list[str], user: User
) -> list[SavedQuery]:
    """Fetch many saved queries in one statement, in the order asked for.

    A dashboard card resolves to a saved query, and a board may span several
    connections, so the per-connection listing cannot serve one. Without this
    the frontend issues one GET per card: a twelve-card board was thirteen
    round trips before first paint, each with its own app-state session.

    Unknown ids are skipped rather than raising, because a board that has just
    lost a query should still render the cards that survived. An id that
    belongs to somebody else is skipped the same way, on purpose: an id list
    is exactly the kind of thing that gets pasted into a chat, and this is a
    batch-fetch endpoint, not a per-id lookup, so there is no natural "not
    found" response to hand back for one entry among many - filtering it out
    of the result is what "skip what you cannot show" already means here.
    """
    if not query_ids:
        return []

    found = {
        query.id: query
        for query in session.scalars(
            select(SavedQuery)
            .where(SavedQuery.id.in_(query_ids), visible_to(user))
            # The read model includes each query's charts, so without this the
            # batch fetch that exists to be one statement becomes one plus one
            # per query - which is exactly what it was written to avoid.
            .options(selectinload(SavedQuery.charts))
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

    Preview rows have no ``query_id`` and are skipped here rather than falling
    out as a NULL group: ``WHERE query_id = NULL`` matches nothing, so the
    group would cost a query per prune and delete nothing. This is a *depth
    per query* bound, and a row belonging to no query has no depth to bound;
    ``log_retention_days`` is what keeps those from accumulating.
    """
    over_quota = session.execute(
        select(QueryExecutionLog.query_id)
        .where(QueryExecutionLog.query_id.is_not(None))
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
