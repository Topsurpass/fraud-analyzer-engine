"""Dashboard CRUD.

A dashboard groups saved queries into one view. It holds no SQL of its own and
never touches a target database - it is an arrangement, and every card on it
resolves through the saved-query endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.schemas.dashboard import DashboardCreate, DashboardRead, DashboardUpdate
from app.services import dashboard_service as svc

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


def _read(dashboard) -> DashboardRead:
    """Serialise, flattening the association rows into an ordered id list."""
    return DashboardRead(
        id=dashboard.id,
        name=dashboard.name,
        query_ids=dashboard.query_ids,
        created_at=dashboard.created_at,
        updated_at=dashboard.updated_at,
    )


@router.get("", response_model=list[DashboardRead])
def list_dashboards(session: Session = Depends(get_session)) -> list[DashboardRead]:
    """Every dashboard, oldest first."""
    return [_read(d) for d in svc.list_dashboards(session)]


@router.post("", response_model=DashboardRead, status_code=status.HTTP_201_CREATED)
def create_dashboard(
    payload: DashboardCreate, session: Session = Depends(get_session)
) -> DashboardRead:
    """Create a dashboard, optionally populated in one call.

    Every referenced query must exist; nothing is persisted otherwise.
    """
    return _read(svc.create_dashboard(session, payload))


@router.get("/{dashboard_id}", response_model=DashboardRead)
def get_dashboard(
    dashboard_id: str, session: Session = Depends(get_session)
) -> DashboardRead:
    return _read(svc.get_dashboard(session, dashboard_id))


@router.put("/{dashboard_id}", response_model=DashboardRead)
def update_dashboard(
    dashboard_id: str,
    payload: DashboardUpdate,
    session: Session = Depends(get_session),
) -> DashboardRead:
    """Rename a dashboard, reorder it, or replace what is on it.

    ``query_ids`` replaces the whole arrangement rather than merging into it.
    """
    dashboard = svc.get_dashboard(session, dashboard_id)
    return _read(svc.update_dashboard(session, dashboard, payload))


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard(
    dashboard_id: str, session: Session = Depends(get_session)
) -> Response:
    """Delete the board. The saved queries it showed are untouched."""
    svc.delete_dashboard(session, svc.get_dashboard(session, dashboard_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
