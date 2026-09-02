"""Dashboard CRUD.

A dashboard groups saved queries into one view. It holds no SQL of its own and
never touches a target database - it is an arrangement, and every card on it
resolves through the saved-query endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.models.user import User
from app.schemas.dashboard import DashboardCreate, DashboardRead, DashboardUpdate
from app.security.deps import require_user
from app.services import dashboard_service as svc

router = APIRouter(
    prefix="/dashboards",
    tags=["dashboards"],
    dependencies=[Depends(require_user)],
)


def _read(dashboard) -> DashboardRead:
    """Serialise, flattening the association rows into an ordered id list."""
    return DashboardRead(
        id=dashboard.id,
        name=dashboard.name,
        chart_ids=dashboard.chart_ids,
        charts=[item.chart for item in sorted(dashboard.items, key=lambda i: i.position)],
        owner_id=dashboard.owner_id,
        owner_name=dashboard.owner.full_name if dashboard.owner else None,
        owner_email=dashboard.owner.email if dashboard.owner else None,
        created_at=dashboard.created_at,
        updated_at=dashboard.updated_at,
    )


@router.get("", response_model=list[DashboardRead])
def list_dashboards(
    user: User = Depends(require_user), session: Session = Depends(get_session)
) -> list[DashboardRead]:
    """Every dashboard the caller may see, oldest first."""
    return [_read(d) for d in svc.list_dashboards(session, user)]


@router.post("", response_model=DashboardRead, status_code=status.HTTP_201_CREATED)
def create_dashboard(
    payload: DashboardCreate,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> DashboardRead:
    """Create a dashboard, optionally populated in one call.

    Every referenced query must exist; nothing is persisted otherwise.
    """
    return _read(svc.create_dashboard(session, payload, user))


@router.get("/{dashboard_id}", response_model=DashboardRead)
def get_dashboard(
    dashboard_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> DashboardRead:
    return _read(svc.get_owned(session, dashboard_id, user))


@router.put("/{dashboard_id}", response_model=DashboardRead)
def update_dashboard(
    dashboard_id: str,
    payload: DashboardUpdate,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> DashboardRead:
    """Rename a dashboard, reorder it, or replace what is on it.

    ``chart_ids`` replaces the whole arrangement rather than merging into it.
    """
    dashboard = svc.get_owned(session, dashboard_id, user)
    return _read(svc.update_dashboard(session, dashboard, payload, user))


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard(
    dashboard_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Delete the board. The saved queries it showed are untouched."""
    svc.delete_dashboard(session, svc.get_owned(session, dashboard_id, user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
