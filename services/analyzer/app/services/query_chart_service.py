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

from app.models import QueryChart, SavedQuery
from app.services import result_cache


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
