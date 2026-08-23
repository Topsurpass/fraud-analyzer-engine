"""Dashboard lifecycle.

A dashboard is an ordered arrangement of saved queries. The engine owns the
arrangement so it is shared: every browser and every machine sees the same
boards, and a query deleted anywhere disappears from all of them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import DuplicateNameError, ErrorCode, NotFoundError
from app.models import Dashboard, DashboardItem, SavedQuery
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
    return list(session.scalars(select(Dashboard).order_by(Dashboard.created_at)))


def _validate_query_ids(session: Session, query_ids: list[str]) -> list[str]:
    """Reject unknown ids, and collapse duplicates keeping first position.

    A dashboard referencing a query that does not exist would render a card
    that can only ever error, so the reference is checked at write time rather
    than discovered at read time. Duplicates are always a mistake: the same
    card twice on one board conveys nothing.
    """
    deduped: list[str] = []
    for query_id in query_ids:
        if query_id not in deduped:
            deduped.append(query_id)

    if deduped:
        found = set(
            session.scalars(select(SavedQuery.id).where(SavedQuery.id.in_(deduped)))
        )
        missing = [query_id for query_id in deduped if query_id not in found]
        if missing:
            raise NotFoundError(
                ErrorCode.QUERY_NOT_FOUND,
                f"No saved query with id {missing[0]!r}.",
                {"query_ids": missing},
            )

    return deduped


def _apply_items(dashboard: Dashboard, query_ids: list[str]) -> None:
    """Replace the arrangement wholesale.

    Rebuilding is simpler than diffing and cannot leave a gap or a duplicate
    position behind, which is what makes ``position`` safe to trust as the
    display order.
    """
    dashboard.items.clear()
    for position, query_id in enumerate(query_ids):
        dashboard.items.append(DashboardItem(query_id=query_id, position=position))


def _commit(session: Session, dashboard: Dashboard, name: str) -> Dashboard:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(
            f"A dashboard named {name!r} already exists.",
            {"name": name},
        ) from exc
    session.refresh(dashboard)
    return dashboard


def create_dashboard(session: Session, payload: DashboardCreate) -> Dashboard:
    query_ids = _validate_query_ids(session, payload.query_ids)
    dashboard = Dashboard(name=payload.name)
    _apply_items(dashboard, query_ids)
    session.add(dashboard)
    return _commit(session, dashboard, payload.name)


def update_dashboard(
    session: Session, dashboard: Dashboard, payload: DashboardUpdate
) -> Dashboard:
    data = payload.model_dump(exclude_unset=True)

    if "query_ids" in data and data["query_ids"] is not None:
        _apply_items(dashboard, _validate_query_ids(session, data["query_ids"]))
    if data.get("name") is not None:
        dashboard.name = data["name"]

    return _commit(session, dashboard, dashboard.name)


def delete_dashboard(session: Session, dashboard: Dashboard) -> None:
    """Remove the board. The saved queries it showed are left alone."""
    session.delete(dashboard)
    session.commit()
