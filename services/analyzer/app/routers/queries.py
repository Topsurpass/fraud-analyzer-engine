"""Saved-query CRUD and the execution endpoints a dashboard polls."""

from __future__ import annotations

import gzip
import logging

import orjson

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.app_state import get_session, get_sessionmaker
from app.errors import AppError, ErrorCode
from app.models import utcnow
from app.models.saved_query import SavedQuery
from app.models.user import User
from app.schemas.query import (
    BatchPollRequest,
    BatchPollResponse,
    ExecutionLogRead,
    PollChanged,
    PollUnchanged,
    PreviewRequest,
    PreviewResponse,
    QueryChartRead,
    QueryChartSetRead,
    QueryChartSetUpdate,
    RunResponse,
    SavedQueryCreate,
    SavedQueryRead,
    SavedQueryUpdate,
)
from app.security.deps import require_user
from app.services import (
    connection_service,
    query_chart_service,
    flag_dismissal_service,
    flagged_row_service,
    refresher,
    flagging,
    query_service,
    rendered_cache,
    result_cache,
)
from app.services import saved_query_service as svc

logger = logging.getLogger(__name__)

connection_scoped = APIRouter(
    prefix="/connections", tags=["queries"], dependencies=[Depends(require_user)]
)
query_scoped = APIRouter(
    prefix="/queries", tags=["queries"], dependencies=[Depends(require_user)]
)


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
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> SavedQueryRead:
    """Save a query.

    The SQL is validated as read-only, then dry-run with a one-row cap against
    this connection. Nothing is persisted unless both succeed, so a saved query
    is always one that actually runs.
    """
    conn = connection_service.get_connection(session, connection_id)
    return SavedQueryRead.model_validate(
        svc.create_query(session, conn, payload, owner_id=user.id)
    )


@connection_scoped.get("/{connection_id}/queries", response_model=list[SavedQueryRead]) # pyright: ignore[reportIndexIssue]
def list_queries(
    connection_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[SavedQueryRead]:
    """List every saved query on this connection that the caller may see."""
    connection_service.get_connection(session, connection_id)
    return [
        SavedQueryRead.model_validate(q)
        for q in svc.list_queries(session, connection_id, user)
    ]


@query_scoped.get("", response_model=list[SavedQueryRead])  # pyright: ignore[reportIndexIssue]
def list_queries_by_ids(
    ids: str | None = Query(
        default=None,
        description=(
            "Comma-separated saved-query ids, in the order you want them back. "
            "Omit to list every query visible to the caller."
        ),
    ),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[SavedQueryRead]:
    """Resolve many saved queries in one request, or list every query the
    caller may see when no ``ids`` are given.

    A dashboard card resolves to a saved query and a board may span several
    connections, so the per-connection listing cannot serve one. Without this
    the frontend issues one GET per card, and a twelve-card board is thirteen
    round trips before it can paint anything.

    Unknown ids are omitted rather than raising: a board that just lost a query
    should still render the cards that survived. An id belonging to somebody
    else's query is omitted the same way - see ``list_queries_by_ids`` in the
    service for why that is the right answer for a batch endpoint.
    """
    if ids is None:
        return [
            SavedQueryRead.model_validate(q)
            for q in svc.list_visible_queries(session, user)
        ]
    requested = [part.strip() for part in ids.split(",") if part.strip()]
    return [
        SavedQueryRead.model_validate(q)
        for q in svc.list_queries_by_ids(session, requested, user)
    ]


@query_scoped.get("/{query_id}", response_model=SavedQueryRead)
def get_query(
    query_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> SavedQueryRead:
    return SavedQueryRead.model_validate(svc.get_owned(session, query_id, user))


@query_scoped.put("/{query_id}", response_model=SavedQueryRead)
def update_query(
    query_id: str,
    payload: SavedQueryUpdate,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> SavedQueryRead:
    """Update a saved query. New SQL is re-validated and re-dry-run.

    Refused while any of the query's charts is published: colleagues are
    reading those charts, and letting the definition change under them is the
    drift publishing exists to prevent. An admin may edit regardless.
    """
    query = svc.get_owned(session, query_id, user)
    query_chart_service.guard_frozen(session, query, user)
    return SavedQueryRead.model_validate(svc.update_query(session, query, payload))


@query_scoped.delete("/{query_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_query(
    query_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    query = svc.get_owned(session, query_id, user)
    # Deleting a published query would empty the board for everybody reading
    # it, which is a louder version of the same drift the freeze prevents.
    query_chart_service.guard_frozen(session, query, user)
    svc.delete_query(session, query)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@query_scoped.post("/charts/{chart_id}/publish", response_model=QueryChartRead)
def publish_chart(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Share one chart with every signed-in user.

    An analyst may publish a chart on a query they own; an admin may publish
    anyone's. Publishing freezes the query behind it, so a colleague reading
    the chart cannot have the definition changed under them.
    """
    return QueryChartRead.model_validate(
        query_chart_service.publish(session, chart_id, user)
    )


@query_scoped.post("/charts/{chart_id}/unpublish", response_model=QueryChartRead)
def unpublish_chart(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Retract a publication, unfreezing the query behind it.

    Whoever published may unpublish, and an admin always may. So an analyst can
    retract their own, edit, and republish - viewers see the chart leave and
    return changed, which is visible rather than silent. A chart an admin
    published stays the admin's to retract.
    """
    return QueryChartRead.model_validate(
        query_chart_service.unpublish(session, chart_id, user)
    )


@query_scoped.get("/charts/{chart_id}/poll")
def poll_published_chart(
    chart_id: str,
    since_hash: str | None = Query(default=None),
    force: bool = Query(default=False, description="Bypass the cache"),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    """The rows behind one published chart, for anybody signed in.

    A published chart that a viewer cannot poll renders an empty card, so
    sharing the chart without sharing its result would be sharing nothing.
    This is the only read path in the app that deliberately ignores ownership,
    and it is narrow on purpose: the chart must actually be published, and
    what comes back is trimmed to that one chart.

    Two things are deliberately withheld. The SQL text never appears in a run
    payload at all, so a viewer sees the result without the query behind it.
    And the payload's chart list is filtered to the published chart alone,
    because the query's other charts were not shared and a viewer has no
    business learning they exist.
    """
    chart = query_chart_service.get_published(session, chart_id)
    payload = _poll_one(session, chart.query, since_hash, force, user)

    charts = payload.get("charts")
    if isinstance(charts, list):
        payload = {
            **payload,
            "charts": [spec for spec in charts if spec.get("id") == chart_id],
        }
    return payload


@query_scoped.get("/charts/published", response_model=list[QueryChartRead])
def list_published_charts(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[QueryChartRead]:
    """Every published chart, whoever built it.

    Deliberately unfiltered by owner: published means published, and escaping
    the owner-only rule is the entire point of the feature.
    """
    return [
        QueryChartRead.model_validate(chart)
        for chart in query_chart_service.list_published(session)
    ]


@query_scoped.get("/{query_id}/logs", response_model=list[ExecutionLogRead])
def list_logs(
    query_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[ExecutionLogRead]:
    """Recent execution attempts, newest first. Useful for debugging a chart."""
    svc.get_owned(session, query_id, user)
    return [
        ExecutionLogRead.model_validate(entry)
        for entry in svc.recent_logs(session, query_id, limit)
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _to_run_response(payload, poll_interval_ms: int) -> dict:
    return payload.as_dict(poll_interval_ms)


def _execute_and_log(session: Session, query, conn, user: User):
    """Run a saved query, recording the attempt either way.

    ``user`` is who asked for this run, threaded all the way down to the log
    row so "who ran this" is answerable later. It is never None for these two
    call sites - both sit behind ``require_user``. Every other path that
    executes against a target carries its caller too, including the ones that
    finish after the response: the stale-cache refresher takes the id of the
    analyst whose poll started it, and the flagged refresh the id of whoever
    clicked. Only ``app/services/scheduler.py`` logs with no user, because a
    timer really has nobody behind it.
    """
    try:
        payload = query_service.run_saved_query(query, conn)
    except AppError as error:
        svc.log_execution(session, query.id, success=False, error=error, user_id=user.id)
        raise
    svc.log_execution(
        session,
        query.id,
        success=True,
        row_count=payload.row_count,
        duration_ms=payload.duration_ms,
        user_id=user.id,
    )
    # Every actual execution updates the stored queue, so a finding outlives
    # the cache entry that produced it and the flagged view has something to
    # show without re-running anything.
    flagged_row_service.sync(session, query, payload.columns, payload.rows, payload.flags)
    return payload


@query_scoped.post("/{query_id}/run", response_model=RunResponse)
def run_query(
    query_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    """Execute a saved query now.

    Always hits the target database and refreshes the poll cache, so this is
    the way to force a fresh read.
    """
    query = svc.get_owned(session, query_id, user)
    conn = connection_service.get_connection(session, query.connection_id)

    payload = _execute_and_log(session, query, conn, user)
    interval = query_service.poll_interval_for(query)
    body = _to_run_response(payload, interval)
    # Cached before filtering, served after: see apply_dismissals.
    result_cache.set(query.id, payload.data_hash, body, ttl_ms=interval)
    return flag_dismissal_service.apply_dismissals(
        body, flag_dismissal_service.dismissed_fingerprints(session, query.id)
    )


@query_scoped.get("/{query_id}/poll", response_model=PollChanged | PollUnchanged) # pyright: ignore[reportGeneralTypeIssues]
def poll_query(
    request: Request,
    query_id: str,
    since_hash: str | None = Query(default=None),
    force: bool = Query(default=False, description="Bypass the cache"),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Cheap endpoint for a dashboard's interval loop.

    Inside the cache TTL this compares hashes in memory and never opens a
    connection to the target database. Without that, a five-second interval
    would run 720 real queries per hour per chart.

    Returns ``changed: false`` when the current hash equals ``since_hash``, and
    the full payload otherwise.
    """
    body = _poll_one(session, svc.get_owned(session, query_id, user), since_hash, force, user)
    # An unchanged answer carries no rows and is a few dozen bytes, so it goes
    # out the ordinary way. Only the payload-carrying answer is worth serving
    # from pre-encoded bytes.
    if not body.get("changed", False):
        return Response(content=orjson.dumps(body), media_type="application/json")
    return _rendered_response(request, body, query_id)


def _rendered_response(request: Request, body: dict, query_id: str) -> Response:
    """Serve a poll body from pre-encoded, pre-gzipped bytes.

    The whole point of the rendered cache: for a given result, these bytes can
    only ever be one thing, so the second and every later request for them - the
    next tick of the same card, and every other analyst watching the same board
    - costs a dict lookup and a socket write instead of re-validating and
    re-serialising 225,000 cells.

    Returning a ``Response`` also skips FastAPI's response-model round trip.
    That is not incidental: on a 25,000-row payload the validate-and-re-encode
    pass was the single largest slice of a warm poll. The ``response_model`` on
    the route is kept so the OpenAPI contract still documents the shape.
    """
    interval = body.get("poll_interval_ms", 0)
    key = rendered_cache.key_for(
        query_id, body["data_hash"], interval, bool(body.get("from_cache"))
    )
    rendered = rendered_cache.get_or_render(key, lambda: body)

    if not rendered.compressed:
        return Response(content=rendered.body, media_type="application/json")

    # Every browser sends this. curl and the odd script do not, and they get
    # the bytes decompressed on the way out, which is still cheaper than
    # rebuilding them from the payload.
    if "gzip" not in request.headers.get("accept-encoding", ""):
        return Response(content=gzip.decompress(rendered.body), media_type="application/json")

    return Response(
        content=rendered.body,
        media_type="application/json",
        # Set explicitly so GZipMiddleware passes the body through untouched
        # rather than compressing bytes that are already compressed.
        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
    )


def _poll_one(
    session: Session,
    query: SavedQuery,
    since_hash: str | None,
    force: bool,
    user: User,
) -> dict:
    """The whole poll decision for one query.

    Takes an already-resolved query rather than an id, because callers earn
    the right to it in different ways: an owner through ``get_owned``, a
    viewer of a published chart through the chart being public. Keeping the
    resolution outside means the caching, hashing and logging below cannot
    drift between those paths.
    """
    query_id = query.id
    interval = query_service.poll_interval_for(query)
    # Read once and applied to whichever payload is served. A row the analyst
    # has reviewed should stop being marked on the chart too, not only in the
    # flagged view - a card still showing it red is telling them there is work
    # left that they have already done.
    dismissed = flag_dismissal_service.dismissed_fingerprints(session, query_id)

    # A fresh entry answers outright. A stale one answers *and* starts a
    # refresh behind the response: the reader gets the last known chart
    # immediately instead of waiting on the target database, and the next poll
    # picks up the new one when it lands. Only a query nobody has ever run
    # blocks, and only once.
    cached = None if force else result_cache.get(query_id)
    if cached is None and not force:
        stale = result_cache.get_stale(query_id)
        if stale is not None:
            # The person polling is the person who caused the refresh, even
            # though it lands after their response does.
            refresher.request_refresh(query_id, user.id)
            cached = stale

    if cached is not None:
        body = flag_dismissal_service.apply_dismissals(cached.payload, dismissed)
        if since_hash and body["data_hash"] == since_hash:
            return PollUnchanged(
                query_id=query_id,
                data_hash=body["data_hash"],
                poll_interval_ms=interval,
                from_cache=True,
            ).model_dump()
        return {**body, "changed": True, "from_cache": True}

    conn = connection_service.get_connection(session, query.connection_id)
    payload = _execute_and_log(session, query, conn, user)
    body = _to_run_response(payload, interval)
    # Cached unfiltered, on purpose: a later dismissal has to be able to change
    # what this payload looks like without the query being run again.
    result_cache.set(query_id, payload.data_hash, body, ttl_ms=interval)
    body = flag_dismissal_service.apply_dismissals(body, dismissed)

    if since_hash and body["data_hash"] == since_hash:
        return PollUnchanged(
            query_id=query_id,
            data_hash=body["data_hash"],
            poll_interval_ms=interval,
            from_cache=False,
        ).model_dump()
    return {**body, "changed": True, "from_cache": False}


@connection_scoped.post("/{connection_id}/query/preview", response_model=PreviewResponse)
def preview_query(
    connection_id: str,
    payload: PreviewRequest,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> PreviewResponse:
    """Run ad-hoc SQL without saving it, for a 'try before you save' UX.

    Goes through the same guard as everything else and is capped aggressively,
    since this is exploratory only. Nothing is *persisted* - no saved query,
    no cache entry, no flagged rows - but the run itself is logged, with the
    caller's id and the connection it touched. Exploratory or not, this is
    analyst-authored SQL against a customer's database, which is precisely the
    thing the execution log exists to attribute; "we did not save the query"
    is not a reason to leave no record that it ran. There is no saved query to
    hang the row on, so it carries ``connection_id`` instead - see
    ``QueryExecutionLog.connection_id``.
    """
    conn = connection_service.get_connection(session, connection_id)
    settings = get_settings()
    requested = payload.row_limit or settings.preview_row_limit
    row_limit = min(query_service.resolve_row_limit(requested), settings.preview_row_limit)

    try:
        result = query_service.execute_sql(conn, payload.sql_text, row_limit=row_limit)
    except AppError as error:
        # A refused or broken statement still reached the target. A log holding
        # only successes hides exactly the runs somebody goes looking for.
        svc.log_execution(
            session,
            None,
            success=False,
            error=error,
            user_id=user.id,
            connection_id=conn.id,
        )
        raise
    svc.log_execution(
        session,
        None,
        success=True,
        row_count=result.row_count,
        duration_ms=result.duration_ms,
        user_id=user.id,
        connection_id=conn.id,
    )
    return PreviewResponse(
        connection_id=conn.id,
        executed_at=utcnow(),
        duration_ms=result.duration_ms,
        row_count=result.row_count,
        truncated=result.truncated,
        columns=result.columns,
        rows=result.rows,
        flags=preview_flags(payload.flag_rules, result.columns, result.rows),
    )


def preview_flags(rules, columns: list[str], rows: list[list]) -> dict:
    """Evaluate unsaved rules against preview rows.

    The rules have no database identity yet, so each is given its index as an
    id. That is enough for the editor to map a flagged row back to the rule
    the user is editing, and nothing outside this response ever sees it.
    """
    if not rules:
        return flagging.FlagOutcome().as_dict()

    specs = [
        flagging.RuleSpec(
            id=str(position),
            name=rule.name,
            severity=rule.severity,
            enabled=rule.enabled,
            conditions=tuple(
                flagging.ConditionSpec(
                    column_name=condition.column_name,
                    operator=condition.operator,
                    value=condition.value,
                    value2=condition.value2,
                )
                for condition in rule.conditions
            ),
        )
        for position, rule in enumerate(rules)
    ]
    return flagging.evaluate(specs, columns, rows).as_dict()


@query_scoped.post("/poll", response_model=BatchPollResponse)
def poll_queries(
    payload: BatchPollRequest,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    """Poll many saved queries in one request.

    Every chart on a dashboard runs its own interval loop. Twelve cards at the
    five-second default is twelve concurrent requests every five seconds, each
    taking a worker thread and an app-state session, and each taking a target
    connection out of a pool of ten whenever it misses cache. That load is
    structural, not incidental, and this is the endpoint that removes it: one
    request per board per tick instead of one per card.

    A failure is reported per query rather than failing the batch, so one card
    with broken SQL cannot blank out the eleven beside it. That includes a
    card belonging to somebody else: QUERY_NOT_FOUND lands in that card's slot
    in the results, not as a 404 for the whole batch.
    """
    return {"results": _poll_many(payload.queries, payload.force, user)}


def _poll_item(item, force: bool, user: User) -> dict:
    """One query's slot in a batch, on its own app-state session.

    A Session is not thread-safe and must not be shared across the workers
    below, so each call opens and closes its own. That is one extra checkout
    from the app-state pool per card, which is cheap next to what it buys: the
    cards stop queueing behind each other.
    """
    session = get_sessionmaker()()
    try:
        return _poll_one(
            session,
            svc.get_owned(session, item.query_id, user),
            item.since_hash,
            force,
            user,
        )
    except AppError as error:
        # Reported in this card's slot rather than as a 404 for the whole
        # batch: eleven working cards must not go blank because the twelfth
        # has broken SQL or belongs to somebody else.
        return {
            "query_id": item.query_id,
            "ok": False,
            "error_code": error.error_code.value,
            "message": error.message,
            "detail": error.detail,
        }
    except Exception as error:  # noqa: BLE001
        # A worker thread that raises anything else would otherwise surface as
        # a 500 for the entire board. One card's unexpected failure is still
        # one card's failure.
        logger.exception("Unexpected failure polling %s in a batch.", item.query_id)
        return {
            "query_id": item.query_id,
            "ok": False,
            "error_code": ErrorCode.INTERNAL_ERROR.value,
            "message": "This card could not be refreshed.",
            "detail": None,
        }
    finally:
        session.close()


def _poll_many(items, force: bool, user: User) -> list[dict]:
    """Poll a batch, one card after another.

    ## Why this is not parallel

    It was, briefly, and the measurements said no. Eight cards of 25,000 rows,
    same code otherwise (bench/http_poll.py):

        workers   cold batch   warm batch   cpu (cold)
        1            1596 ms       222 ms      1596 ms
        2            3375 ms       310 ms      3984 ms
        4           19782 ms       631 ms     27977 ms

    Not a wash, and not sublinear: strictly and steeply worse, with CPU rising
    faster than wall clock. The work in a poll is per-cell Python - coercing
    225,000 values, hashing them, encoding them - so it holds the GIL almost
    end to end. Threads cannot overlap that; they only add contention, and the
    app-state writes each execution makes then serialise on top of it.

    The instinct that a board should not wait for the sum of its cards is
    right. Threading the server was the wrong lever for it. The right ones,
    both applied here, are to make each poll cheap enough that the sum is small
    (a warm poll went from 42.8 ms to 6.0 ms via rendered_cache) and to let
    each card own its own request so it paints when its own data lands, which
    is a frontend change rather than a server one.

    Kept in one function rather than inlined so the reasoning above has
    somewhere to live, and so a future attempt at parallelism has to read it.
    """
    return [_poll_item(item, force, user) for item in items]


@query_scoped.get("/{query_id}/charts", response_model=QueryChartSetRead)
def get_charts(
    query_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartSetRead:
    """Every way this query's result can be drawn, in display order."""
    svc.get_owned(session, query_id, user)
    return QueryChartSetRead(
        query_id=query_id, charts=query_chart_service.list_charts(session, query_id)
    )


@query_scoped.put("/{query_id}/charts", response_model=QueryChartSetRead)
def put_charts(
    query_id: str,
    payload: QueryChartSetUpdate,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartSetRead:
    """Replace a query's whole chart set.

    Charts are matched by name, so editing a chart's type or fields keeps its
    id and every dashboard placing it keeps working. Renaming one reads as
    removing it and adding another, which is the honest interpretation: a board
    placing "Trend" cannot know the thing now called "Volume" is the same
    intent.

    Adding a chart costs nothing at the target database. They all render from
    one already-fetched result, which is the entire point of separating them
    from the query.
    """
    query = svc.get_owned(session, query_id, user)
    # Replacing the set can rewire a published chart's fields or drop it
    # entirely, both of which change what viewers see.
    query_chart_service.guard_frozen(session, query, user)
    charts = query_chart_service.replace_charts(session, query, payload.charts)
    return QueryChartSetRead(query_id=query_id, charts=charts)
