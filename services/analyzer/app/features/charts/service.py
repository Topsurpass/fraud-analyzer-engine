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
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.errors import AppError, ErrorCode
from app.features.charts.models import QueryChart
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
    """The published charts that make a query un-editable, newest first.

    Whether a query is frozen is *derived* from its charts rather than stored
    as a flag on the query itself. A stored flag would be a second source of
    truth, and the day it disagreed with the charts it describes the symptom
    would be either an un-editable query nobody can explain or a published
    chart that silently drifts.
    """
    return list(
        session.scalars(
            select(QueryChart)
            .where(QueryChart.query_id == query_id, QueryChart.is_public.is_(True))
            .order_by(QueryChart.published_at.desc())
        )
    )


def guard_frozen(session: Session, query: SavedQuery, user: User) -> None:
    """Refuse an edit to a query that has a published chart.

    An admin may edit regardless: they are the approving authority, and
    requiring them to unpublish their own approval first is ceremony.

    The refusal names the way out rather than only the rule. An analyst who is
    blocked is told which chart is published and that unpublishing it unfreezes
    the query, because "this query is frozen" without that sentence sends
    somebody hunting for a setting that does not exist.
    """
    if user.is_admin:
        return

    published = frozen_by(session, query.id)
    if not published:
        return

    names = ", ".join(chart.name for chart in published)
    raise AppError(
        ErrorCode.QUERY_FROZEN,
        f"This query is published as {names}. Unpublish it to edit the query.",
        {"published_charts": [chart.id for chart in published]},
    )


def publish(session: Session, chart_id: str, user: User) -> QueryChart:
    """Make one chart visible to every signed-in user.

    An analyst may publish a chart on a query they own; an admin may publish
    anyone's. Ownership is checked through the query rather than the chart,
    because a chart has no owner of its own - it inherits one, and inventing a
    second would create two answers to the same question.
    """
    chart = session.get(QueryChart, chart_id)
    if chart is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such chart.")

    # get_owned raises QUERY_NOT_FOUND for somebody else's, which is the right
    # answer here too: confirming a chart exists but is not yours tells an
    # analyst what a colleague is working on.
    saved_query_service.get_owned(session, chart.query_id, user)

    if not chart.is_public:
        chart.is_public = True
        chart.published_by = user.id
        chart.published_at = utcnow()
        session.commit()
        redraw_cached(session, chart.query_id)
    return chart


def unpublish(session: Session, chart_id: str, user: User) -> QueryChart:
    """Retract a publication, which also unfreezes the query behind it.

    Whoever published may unpublish. An admin may always unpublish. So an
    analyst can retract their own publication, edit, and republish - viewers
    see the chart leave the board and come back changed, which is visible
    rather than silent, and silent change was the actual risk. A chart an
    admin published stays the admin's to retract, which is what makes an
    admin's freeze over someone else's work real rather than advisory.
    """
    chart = session.get(QueryChart, chart_id)
    if chart is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such chart.")

    saved_query_service.get_owned(session, chart.query_id, user)

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
