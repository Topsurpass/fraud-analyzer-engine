"""Storage for a query's charts.

Whole-set replace rather than per-chart CRUD, matching the flag-rule editor:
the editor edits every chart of a query at once, ``position`` is simply the
index in the incoming list, and no reorder endpoint has to exist or be kept
consistent.

The one thing that cannot be careless is chart identity. Dashboards place a
chart by id, so replacing a set naively - delete all, insert all - would hand
every chart a new id and silently empty every board that showed one. Charts are
therefore matched by name and updated in place; only genuinely new names get new
ids, and only genuinely removed ones are deleted.
"""

from __future__ import annotations

import hmac
from typing import NoReturn

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.db.base import utcnow
from app.enums import AuditAction
from app.errors import AppError, ErrorCode
from app.features.audit import service as audit_service
from app.features.charts.fingerprint import definition_fingerprint
from app.features.charts.models import QueryChart
from app.features.connections.models import Connection
from app.features.flag_rules.models import FlagRule
from app.features.queries import execution as query_service
from app.features.queries import result_cache
from app.features.queries import service as saved_query_service
from app.features.queries.models import SavedQuery
from app.features.users.models import User


def list_charts(session: Session, query_id: str) -> list[QueryChart]:
    """Every chart on a query, in display order."""
    statement = (
        select(QueryChart)
        .where(QueryChart.query_id == query_id)
        .order_by(QueryChart.position)
    )
    return list(session.scalars(statement))


def replace_charts(session: Session, query: SavedQuery, charts: list) -> list[QueryChart]:
    """Replace a query's whole chart set, preserving ids where names match.

    Renaming a chart therefore reads as deleting one and adding another, which
    is the honest interpretation: a board placing "Trend" cannot know that the
    thing now called "Volume" is the same intent. Editing a chart's *type* or
    *fields* keeps its id, so a board keeps showing it and simply draws it
    differently.
    """
    existing = {chart.name.strip().lower(): chart for chart in query.charts}
    kept: set[str] = set()
    built: list[QueryChart] = []

    for position, spec in enumerate(charts):
        key = spec.name.strip().lower()
        chart = existing.get(key)
        if chart is None:
            chart = QueryChart(query_id=query.id, name=spec.name)
            query.charts.append(chart)
        else:
            kept.add(key)
        chart.name = spec.name
        chart.position = position
        chart.chart_type = spec.chart_type
        chart.x_field = spec.x_field
        chart.y_field = spec.y_field
        chart.series_field = spec.series_field
        chart.surge_threshold_pct = spec.surge_threshold_pct
        built.append(chart)

    for key, chart in existing.items():
        if key not in kept:
            query.charts.remove(chart)

    session.flush()
    session.commit()
    session.refresh(query, ["charts"])

    # The cached run payload echoes every chart's mapping, so an edited chart
    # would keep drawing the old way until the entry aged out. Re-drawn in
    # place rather than dropped: dropping it made the next poll run the query
    # again, which a change to how a result is drawn never needs.
    redraw_cached(session, query.id)
    return list_charts(session, query.id)


def redraw_cached(session: Session, query_id: str) -> None:
    """Bring a query's cached result in line with its charts, without a run.

    Polling compares row hashes, and a chart edit leaves the rows alone, so a
    client that polls with its old ``since_hash`` is told "unchanged" and keeps
    the old mapping. A client that has just edited a chart asks for the whole
    payload instead (no ``since_hash``), and gets this one, already redrawn.
    """
    query = session.get(SavedQuery, query_id)
    if query is None:
        result_cache.invalidate(query_id)
        return
    result_cache.patch_charts(
        query_id, lambda columns: query_service.build_charts(query, columns)
    )


def frozen_by(session: Session, query_id: str) -> list[QueryChart]:
    """The charts that make a query un-editable, published first then pending.

    A chart waiting for approval freezes its query for the same reason a
    published one does: the administrator is approving a definition, and an
    author who could change the SQL after asking and before the approver looked
    would be approving one thing and publishing another.

    Whether a query is frozen is *derived* from its charts rather than stored
    as a flag on the query itself. A stored flag would be a second source of
    truth, and the day it disagreed with the charts it describes the symptom
    would be either an un-editable query nobody can explain or a published
    chart that silently drifts.
    """
    charts = session.scalars(
        select(QueryChart).where(
            QueryChart.query_id == query_id,
            (QueryChart.is_public.is_(True)) | (QueryChart.publish_requested_at.is_not(None)),
        )
    )
    return sorted(charts, key=lambda c: (not c.is_public, -(c.published_at or c.publish_requested_at).timestamp()))


def guard_frozen(session: Session, query: SavedQuery, user: User) -> None:
    """Refuse an edit to a query that has a published or pending chart.

    An admin may edit regardless: they are the approving authority, and
    requiring them to unpublish their own approval first is ceremony.

    The refusal names the way out rather than only the rule. An analyst who is
    blocked is told which chart is published and that unpublishing it unfreezes
    the query, or which request is waiting and that withdrawing it does,
    because "this query is frozen" without that sentence sends somebody hunting
    for a setting that does not exist.
    """
    if user.is_admin:
        return

    frozen = frozen_by(session, query.id)
    if not frozen:
        return

    published = [chart for chart in frozen if chart.is_public]
    if published:
        names = ", ".join(chart.name for chart in published)
        message = f"This query is published as {names}. Unpublish it to edit the query."
    else:
        names = ", ".join(chart.name for chart in frozen)
        message = (
            f"A request to publish {names} is waiting for an administrator. "
            "Withdraw the request to edit the query."
        )
    raise AppError(
        ErrorCode.QUERY_FROZEN,
        message,
        {"published_charts": [chart.id for chart in frozen]},
    )


def _chart_for_owner(session: Session, chart_id: str, user: User) -> QueryChart:
    """The chart, if the caller owns its query or is an administrator.

    Ownership is checked through the query rather than the chart, because a
    chart has no owner of its own - it inherits one, and inventing a second
    would create two answers to the same question. ``get_owned`` raises
    QUERY_NOT_FOUND for somebody else's, which is the right answer here too:
    confirming a chart exists but is not yours tells an analyst what a
    colleague is working on.
    """
    chart = session.get(QueryChart, chart_id)
    if chart is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such chart.")
    saved_query_service.get_owned(session, chart.query_id, user)
    return chart


#: Columns that say "nobody is waiting on this chart".
_NO_REQUEST = {"publish_requested_by": None, "publish_requested_at": None}
_NO_REJECTION = {
    "publish_rejected_by": None,
    "publish_rejected_at": None,
    "publish_rejected_reason": None,
}


def _apply_if(session: Session, chart: QueryChart, conditions: tuple, values: dict) -> bool:
    """Change a chart only if it is still in the state the caller decided from.

    Every transition is decided from a row that was read a moment earlier, and an
    administrator's click and an author's withdrawal can land in that gap. Assigning
    attributes and committing would let a stale approve publish a request that was
    withdrawn meanwhile, or a stale withdrawal clear a publication. So the change is a
    single conditional UPDATE and the answer is whether it matched: ``WHERE`` carries
    the state the decision assumed, and a mismatch changes nothing.
    """
    result = session.execute(
        update(QueryChart)
        .where(QueryChart.id == chart.id, *conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


def _audit(session: Session, user: User, action: AuditAction, chart: QueryChart, **extra) -> None:
    audit_service.record(
        session, user, action, "chart", chart.id,
        {"chart": chart.name, "query_id": chart.query_id, **extra}, commit=False,
    )


def _finish(session: Session, chart: QueryChart) -> QueryChart:
    """Commit and re-read, so the caller sees what the database says now."""
    session.commit()
    session.refresh(chart)
    return chart


def publish(session: Session, chart_id: str, user: User) -> QueryChart:
    """Publish a chart, or ask for it to be published.

    An administrator publishes at once, anyone's chart. Everybody else *asks*:
    the chart becomes pending, the query behind it freezes, and an administrator
    decides (``approve`` and ``reject``). An analyst's publish therefore never
    produces a published chart by itself, which is the whole of the approval rule.

    Already pending or already published: nothing changes, so a second click and
    a retried request are harmless and the original requester and time stand.
    """
    chart = _chart_for_owner(session, chart_id, user)
    now = utcnow()

    if user.is_admin:
        if _apply_if(
            session, chart, (QueryChart.is_public.is_(False),),
            {"is_public": True, "published_by": user.id, "published_at": now,
             **_NO_REQUEST, **_NO_REJECTION},
        ):
            _audit(session, user, AuditAction.CHART_PUBLISHED, chart)
            _finish(session, chart)
            redraw_cached(session, chart.query_id)
            return chart
        return _finish(session, chart)

    if _apply_if(
        session, chart,
        (QueryChart.is_public.is_(False), QueryChart.publish_requested_at.is_(None)),
        {"publish_requested_by": user.id, "publish_requested_at": now, **_NO_REJECTION},
    ):
        _audit(session, user, AuditAction.CHART_PUBLISH_REQUESTED, chart)
    return _finish(session, chart)


def cancel_request(session: Session, chart_id: str, user: User) -> QueryChart:
    """Withdraw a pending request, or dismiss a rejection notice.

    Unfreezes the query. A published chart is left alone (retract it with
    ``unpublish``), and a private one with nothing to clear is returned as it is.
    Both halves are conditional on what they clear, so a withdrawal that arrives
    after an administrator approved changes nothing: it cannot clear a publication.
    """
    chart = _chart_for_owner(session, chart_id, user)

    if _apply_if(
        session, chart,
        (QueryChart.is_public.is_(False), QueryChart.publish_requested_at.is_not(None)),
        _NO_REQUEST,
    ):
        _audit(session, user, AuditAction.CHART_PUBLISH_CANCELLED, chart)
    else:
        _apply_if(
            session, chart,
            (
                QueryChart.is_public.is_(False),
                QueryChart.publish_requested_at.is_(None),
                QueryChart.publish_rejected_at.is_not(None),
            ),
            _NO_REJECTION,
        )
    return _finish(session, chart)


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise AppError(ErrorCode.FORBIDDEN, "Only an administrator can decide on a publish request.")


def _pending_chart(session: Session, chart_id: str) -> QueryChart:
    chart = session.get(QueryChart, chart_id)
    if chart is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such chart.")
    if chart.is_public or chart.publish_requested_at is None:
        raise AppError(
            ErrorCode.PUBLISH_NOT_PENDING,
            "Nobody is waiting on this chart: it was already decided, withdrawn, or "
            "never requested.",
        )
    return chart


def _rules_of(session: Session, query_id: str) -> list[FlagRule]:
    return list(
        session.scalars(
            select(FlagRule)
            .where(FlagRule.query_id == query_id)
            .order_by(FlagRule.position)
            .options(selectinload(FlagRule.conditions))
        )
    )


def fingerprint_of(session: Session, chart: QueryChart) -> str:
    """The fingerprint of this chart's definition as it is stored right now."""
    return definition_fingerprint(chart.query, chart, _rules_of(session, chart.query_id))


def _undecided(session: Session, chart_id: str) -> NoReturn:
    """A request that was lost to somebody else between reading it and writing it."""
    session.rollback()
    raise AppError(
        ErrorCode.PUBLISH_NOT_PENDING,
        "Nobody is waiting on this chart: it was decided, withdrawn or asked for "
        "again while you were looking at it.",
    )


def approve(
    session: Session, chart_id: str, user: User, definition_fingerprint_seen: str
) -> QueryChart:
    """An administrator accepts a request: the chart becomes visible to everyone.

    Bound to the definition that was read. ``definition_fingerprint_seen`` is what the
    administrator was shown; if the stored definition no longer hashes to it (the
    author withdrew, edited and asked again while the page was open) the answer is
    ``DEFINITION_CHANGED`` and nothing is published. Whether the request is still
    pending is checked first and wins.

    The write is a conditional update that also requires the request to be the very
    one that was read, so an approval cannot land on a request withdrawn or replaced
    in the meantime.

    ``published_by`` stays the person who asked, not the approver: the author may
    retract their own publication, and an approval does not take that away. (An
    administrator who publishes directly is recorded as the publisher, and then
    only an administrator can retract it.)
    """
    _require_admin(user)
    chart = _pending_chart(session, chart_id)
    requested_at, requester = chart.publish_requested_at, chart.publish_requested_by

    current = fingerprint_of(session, chart)
    if not hmac.compare_digest(current, definition_fingerprint_seen or ""):
        raise AppError(
            ErrorCode.DEFINITION_CHANGED,
            "This chart's definition changed since it was reviewed. Open it again and "
            "review the current SQL and rules before approving.",
            # No fingerprint in here, on purpose: a client that could read the new
            # value out of the refusal could retry without anyone reading the new
            # definition, which is the whole thing this error exists to prevent.
        )

    if not _apply_if(
        session, chart,
        (
            QueryChart.is_public.is_(False),
            QueryChart.publish_requested_at == requested_at,
            QueryChart.publish_requested_by == requester,
        ),
        {"is_public": True, "published_by": requester, "published_at": utcnow(),
         **_NO_REQUEST, **_NO_REJECTION},
    ):
        _undecided(session, chart_id)
    _audit(session, user, AuditAction.CHART_PUBLISH_APPROVED, chart, requested_by=requester)
    _finish(session, chart)
    redraw_cached(session, chart.query_id)
    return chart


def reject(session: Session, chart_id: str, user: User, reason: str | None) -> QueryChart:
    """An administrator declines a request. The chart stays private and the
    author sees why, until they ask again or withdraw. Needs no fingerprint:
    declining publishes nothing, so there is nothing to bind it to."""
    _require_admin(user)
    chart = _pending_chart(session, chart_id)
    requested_at, requester = chart.publish_requested_at, chart.publish_requested_by
    cleaned = (reason or "").strip() or None

    if not _apply_if(
        session, chart,
        (
            QueryChart.is_public.is_(False),
            QueryChart.publish_requested_at == requested_at,
            QueryChart.publish_requested_by == requester,
        ),
        {**_NO_REQUEST, "publish_rejected_by": user.id, "publish_rejected_at": utcnow(),
         "publish_rejected_reason": cleaned},
    ):
        _undecided(session, chart_id)
    _audit(
        session, user, AuditAction.CHART_PUBLISH_REJECTED, chart,
        requested_by=requester, reason=cleaned,
    )
    return _finish(session, chart)


def list_pending(session: Session, user: User) -> list[dict]:
    """Every waiting request, oldest first, for an administrator's queue.

    Each carries the fingerprint of its definition as stored now, which is what
    ``approve`` must be given back.
    """
    _require_admin(user)
    charts = session.scalars(
        select(QueryChart)
        .where(QueryChart.publish_requested_at.is_not(None), QueryChart.is_public.is_(False))
        .order_by(QueryChart.publish_requested_at)
        .options(selectinload(QueryChart.requester), selectinload(QueryChart.query))
    )
    requests = []
    for chart in charts:
        connection = session.get(Connection, chart.query.connection_id)
        requests.append(
            {
                "chart": chart,
                "query_id": chart.query_id,
                "query_name": chart.query.name,
                "connection_id": chart.query.connection_id,
                "connection_name": connection.name if connection is not None else "",
                "requested_by": chart.requester,
                "requested_at": chart.publish_requested_at,
                "definition_fingerprint": fingerprint_of(session, chart),
            }
        )
    return requests


def unpublish(session: Session, chart_id: str, user: User) -> QueryChart:
    """Retract a publication, which also unfreezes the query behind it.

    Whoever published may unpublish. An admin may always unpublish. So an
    analyst can retract their own publication, edit, and republish - viewers
    see the chart leave the board and come back changed, which is visible
    rather than silent, and silent change was the actual risk. A chart an
    admin published stays the admin's to retract, which is what makes an
    admin's freeze over someone else's work real rather than advisory.
    """
    chart = _chart_for_owner(session, chart_id, user)

    if chart.is_public and not user.is_admin and chart.published_by != user.id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "An administrator published this chart, so only an administrator "
            "can unpublish it.",
        )

    conditions = (QueryChart.is_public.is_(True),)
    if not user.is_admin:
        conditions += (QueryChart.published_by == user.id,)
    if _apply_if(
        session, chart, conditions,
        {"is_public": False, "published_by": None, "published_at": None},
    ):
        _audit(session, user, AuditAction.CHART_UNPUBLISHED, chart)
        _finish(session, chart)
        redraw_cached(session, chart.query_id)
        return chart
    return _finish(session, chart)


def list_published(session: Session) -> list[QueryChart]:
    """Every published chart, for the board every signed-in user can see.

    No visibility filter: published means published. The whole point is that
    it escapes the owner-only rule every other read here obeys.
    """
    return list(
        session.scalars(
            select(QueryChart)
            .where(QueryChart.is_public.is_(True))
            .order_by(QueryChart.published_at.desc())
        )
    )


def get_published(session: Session, chart_id: str) -> QueryChart:
    """One published chart, for a viewer who does not own it.

    Deliberately performs no ownership check: being published is the whole
    permission. It does check that the chart is *actually* public, so an id
    guessed or remembered from before an unpublish returns nothing.

    The refusal is ``QUERY_NOT_FOUND`` rather than a distinct "not published"
    code, so a viewer cannot tell an unpublished chart from one that never
    existed. That is the same reasoning behind returning 404 instead of 403
    everywhere else ownership is enforced.
    """
    chart = session.get(QueryChart, chart_id)
    if chart is None or not chart.is_public:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such published chart.")
    return chart


def definition(session: Session, chart_id: str, user: User) -> dict:
    """The query and configuration behind a chart, for somebody who may copy it.

    Allowed for anyone when the chart is published (publishing is the permission),
    and for the author and administrators in every state: an administrator has to
    read a pending request's SQL to decide on it. Anything else is QUERY_NOT_FOUND,
    the same answer as for a chart that does not exist.

    Never touches the connection beyond its name, and never loads a list's items.
    """
    chart = session.get(QueryChart, chart_id)
    if chart is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such chart.")
    query = chart.query
    privileged = user.is_admin or query.owner_id == user.id
    if not chart.is_public and not privileged:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such chart.")

    connection = session.get(Connection, query.connection_id)
    owner = session.get(User, query.owner_id) if query.owner_id else None
    rules = _rules_of(session, query.id)
    return {
        "chart": chart,
        "query": {
            "id": query.id,
            "name": query.name,
            "description": query.description,
            "sql_text": query.sql_text,
            "row_limit": query_service.resolve_row_limit(query.row_limit),
            "poll_interval_ms": query_service.poll_interval_for(query),
        },
        "rules": rules,
        "connection_name": connection.name if connection is not None else "",
        "owner_name": owner.full_name if owner is not None else None,
        "read_only": not privileged,
        # Current, always: the value approve must be given back.
        "definition_fingerprint": definition_fingerprint(query, chart, rules),
    }
