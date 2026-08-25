"""Dashboard lifecycle.

A dashboard is an ordered arrangement of saved queries. The engine owns the
arrangement so it is shared: every browser and every machine sees the same
boards, and a query deleted anywhere disappears from all of them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.errors import DuplicateNameError, ErrorCode, NotFoundError
from app.models import Dashboard, DashboardItem, QueryChart
from app.schemas.dashboard import DashboardCreate, DashboardUpdate


def get_dashboard(session: Session, dashboard_id: str) -> Dashboard:
    dashboard = session.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise NotFoundError(
            ErrorCode.DASHBOARD_NOT_FOUND,
            f"No dashboard with id {dashboard_id!r}.",
            {"dashboard_id": dashboard_id},
        )
    return dashboard


def list_dashboards(session: Session) -> list[Dashboard]:
    """Every dashboard, with its items already loaded.

    ``selectinload`` reaches through to the charts as well, because the read
    model resolves them. Without that second level a board of twenty cards
    lazy-loads twenty charts one at a time - the per-card cost the query/chart
    split exists to remove, moved from the target database to this one.

    It is load-bearing, not a micro-optimisation. ``chart_ids``
    walks ``Dashboard.items``, so serialising a list of N boards lazily emitted
    1 + N statements: five boards measured six round trips. Local SQLite hides
    that entirely; against a managed Postgres at ~10ms per round trip, twenty
    boards is over 200ms of pure latency on a view the frontend loads on every
    page. Two statements now, regardless of how many boards exist.
    """
    return list(
        session.scalars(
            select(Dashboard)
            .options(selectinload(Dashboard.items).selectinload(DashboardItem.chart))
            .order_by(Dashboard.created_at)
        )
    )


def _validate_chart_ids(session: Session, chart_ids: list[str]) -> list[str]:
    """Reject unknown ids, and collapse duplicates keeping first position.

    A dashboard referencing a query that does not exist would render a card
    that can only ever error, so the reference is checked at write time rather
    than discovered at read time. Duplicates are always a mistake: the same
    card twice on one board conveys nothing.
    """
    deduped: list[str] = []
    for chart_id in chart_ids:
        if chart_id not in deduped:
            deduped.append(chart_id)

    if deduped:
        found = set(
            session.scalars(select(QueryChart.id).where(QueryChart.id.in_(deduped)))
        )
        missing = [chart_id for chart_id in deduped if chart_id not in found]
        if missing:
            raise NotFoundError(
                ErrorCode.QUERY_NOT_FOUND,
                f"No chart with id {missing[0]!r}.",
                {"chart_ids": missing},
            )

    return deduped


def _apply_items(dashboard: Dashboard, chart_ids: list[str]) -> None:
    """Replace the arrangement wholesale.

    Rebuilding is simpler than diffing and cannot leave a gap or a duplicate
    position behind, which is what makes ``position`` safe to trust as the
    display order.
    """
    dashboard.items.clear()
    for position, chart_id in enumerate(chart_ids):
        dashboard.items.append(DashboardItem(chart_id=chart_id, position=position))


def _commit(session: Session, dashboard: Dashboard, name: str) -> Dashboard:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(
            f"A dashboard named {name!r} already exists.",
            {"name": name},
        ) from exc
    # refresh() re-SELECTs the whole row on every write. The only thing the
    # caller reads afterwards is the item list, so refresh just that.
    session.refresh(dashboard, ["items"])
    return dashboard


def create_dashboard(session: Session, payload: DashboardCreate) -> Dashboard:
    chart_ids = _validate_chart_ids(session, payload.chart_ids)
    dashboard = Dashboard(name=payload.name)
    _apply_items(dashboard, chart_ids)
    session.add(dashboard)
    return _commit(session, dashboard, payload.name)


def update_dashboard(
    session: Session, dashboard: Dashboard, payload: DashboardUpdate
) -> Dashboard:
    data = payload.model_dump(exclude_unset=True)

    if "chart_ids" in data and data["chart_ids"] is not None:
        _apply_items(dashboard, _validate_chart_ids(session, data["chart_ids"]))
    if data.get("name") is not None:
        dashboard.name = data["name"]

    return _commit(session, dashboard, dashboard.name)


def delete_dashboard(session: Session, dashboard: Dashboard) -> None:
    """Remove the board. The saved queries it showed are left alone."""
    session.delete(dashboard)
    session.commit()
