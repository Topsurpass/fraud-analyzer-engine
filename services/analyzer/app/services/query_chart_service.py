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

from app.errors import AppError, ErrorCode
from app.models import QueryChart, SavedQuery
from app.models.base import utcnow
from app.models.user import User
from app.services import result_cache, saved_query_service


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

    # The cached run payload echoes every chart's mapping, so an edited chart
    # would keep drawing the old way until the entry aged out - and polling
    # would report nothing changed, because the rows did not.
    result_cache.invalidate(query.id)

    session.commit()
    session.refresh(query, ["charts"])
    return list_charts(session, query.id)


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
        result_cache.invalidate(chart.query_id)
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
        result_cache.invalidate(chart.query_id)
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
