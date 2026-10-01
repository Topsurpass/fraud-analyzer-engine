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

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.base import utcnow
from app.enums import AuditAction
from app.errors import AppError, ErrorCode
from app.features.audit import service as audit_service
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


def _clear_request(chart: QueryChart) -> None:
    chart.publish_requested_by = None
    chart.publish_requested_at = None


def _clear_rejection(chart: QueryChart) -> None:
    chart.publish_rejected_by = None
    chart.publish_rejected_at = None
    chart.publish_rejected_reason = None


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
    if chart.is_public:
        return chart

    if user.is_admin:
        chart.is_public = True
        chart.published_by = user.id
        chart.published_at = utcnow()
        _clear_request(chart)
        _clear_rejection(chart)
        audit_service.record(
            session, user, AuditAction.CHART_PUBLISHED, "chart", chart.id,
            {"chart": chart.name, "query_id": chart.query_id}, commit=False,
        )
        session.commit()
        redraw_cached(session, chart.query_id)
        return chart

    if chart.publish_requested_at is not None:
        return chart

    chart.publish_requested_by = user.id
    chart.publish_requested_at = utcnow()
    _clear_rejection(chart)
    audit_service.record(
        session, user, AuditAction.CHART_PUBLISH_REQUESTED, "chart", chart.id,
        {"chart": chart.name, "query_id": chart.query_id}, commit=False,
    )
    session.commit()
    return chart


def cancel_request(session: Session, chart_id: str, user: User) -> QueryChart:
    """Withdraw a pending request, or dismiss a rejection notice.

    Unfreezes the query. A published chart is left alone (retract it with
    ``unpublish``), and a private one with nothing to clear is returned as it is.
    """
    chart = _chart_for_owner(session, chart_id, user)
    if chart.is_public:
        return chart

    if chart.publish_requested_at is not None:
        _clear_request(chart)
        audit_service.record(
            session, user, AuditAction.CHART_PUBLISH_CANCELLED, "chart", chart.id,
            {"chart": chart.name, "query_id": chart.query_id}, commit=False,
        )
        session.commit()
    elif chart.publish_rejected_at is not None:
        _clear_rejection(chart)
        session.commit()
    return chart


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


def approve(session: Session, chart_id: str, user: User) -> QueryChart:
    """An administrator accepts a request: the chart becomes visible to everyone.

    ``published_by`` stays the person who asked, not the approver: the author may
    retract their own publication, and an approval does not take that away. (An
    administrator who publishes directly is recorded as the publisher, and then
    only an administrator can retract it.)
    """
    _require_admin(user)
    chart = _pending_chart(session, chart_id)
    requester = chart.publish_requested_by
    chart.is_public = True
    chart.published_by = requester
    chart.published_at = utcnow()
    _clear_request(chart)
    _clear_rejection(chart)
    audit_service.record(
        session, user, AuditAction.CHART_PUBLISH_APPROVED, "chart", chart.id,
        {"chart": chart.name, "query_id": chart.query_id, "requested_by": requester},
        commit=False,
    )
    session.commit()
    redraw_cached(session, chart.query_id)
    return chart


def reject(session: Session, chart_id: str, user: User, reason: str | None) -> QueryChart:
    """An administrator declines a request. The chart stays private and the
    author sees why, until they ask again or withdraw."""
    _require_admin(user)
    chart = _pending_chart(session, chart_id)
    requester = chart.publish_requested_by
    _clear_request(chart)
    chart.publish_rejected_by = user.id
    chart.publish_rejected_at = utcnow()
    chart.publish_rejected_reason = (reason or "").strip() or None
    audit_service.record(
        session, user, AuditAction.CHART_PUBLISH_REJECTED, "chart", chart.id,
        {"chart": chart.name, "query_id": chart.query_id, "requested_by": requester,
         "reason": chart.publish_rejected_reason},
        commit=False,
    )
    session.commit()
    return chart


def list_pending(session: Session, user: User) -> list[dict]:
    """Every waiting request, oldest first, for an administrator's queue."""
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

    if chart.is_public:
        chart.is_public = False
        chart.published_by = None
        chart.published_at = None
        audit_service.record(
            session, user, AuditAction.CHART_UNPUBLISHED, "chart", chart.id,
            {"chart": chart.name, "query_id": chart.query_id}, commit=False,
        )
        session.commit()
        redraw_cached(session, chart.query_id)
    return chart


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
    rules = session.scalars(
        select(FlagRule)
        .where(FlagRule.query_id == query.id)
        .order_by(FlagRule.position)
        .options(selectinload(FlagRule.conditions))
    )
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
        "rules": list(rules),
        "connection_name": connection.name if connection is not None else "",
        "owner_name": owner.full_name if owner is not None else None,
        "read_only": not privileged,
    }
