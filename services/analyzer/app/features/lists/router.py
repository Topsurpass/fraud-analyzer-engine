"""Named lists: create, read, replace, delete.

Any signed-in user may list, read and use a list in a rule. Replacing or
deleting is for the creator or an administrator (403 otherwise).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.features.lists import service as svc
from app.features.lists.models import ItemList
from app.features.lists.schemas import (
    ItemListRead,
    ItemListSummary,
    ItemListWrite,
    ItemListWriteResult,
)
from app.features.users.models import User
from app.security.deps import require_user

router = APIRouter(
    prefix="/lists",
    tags=["lists"],
    dependencies=[Depends(require_user)],
)


def _summary_fields(item_list: ItemList, stats: svc.ListStats) -> dict:
    return {
        "id": item_list.id,
        "name": item_list.name,
        "description": item_list.description,
        "item_count": stats.item_count,
        "rule_count": stats.rule_count,
        "created_by": item_list.created_by,
        "created_at": item_list.created_at,
        "updated_at": item_list.updated_at,
    }


@router.get("", response_model=list[ItemListSummary])
def list_lists(session: Session = Depends(get_session)) -> list[ItemListSummary]:
    """Every list, by name. Counts only; fetch one list for its items."""
    return [
        ItemListSummary(**_summary_fields(item_list, stats))
        for item_list, stats in svc.list_lists(session)
    ]


@router.post("", response_model=ItemListWriteResult, status_code=status.HTTP_201_CREATED)
def create_list(
    payload: ItemListWrite,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ItemListWriteResult:
    """Create a list. Repeats and blanks are dropped and counted in the reply."""
    item_list, report = svc.create_list(session, payload, user)
    return _write_result(session, item_list, report)


@router.get("/{list_id}", response_model=ItemListRead)
def get_list(list_id: str, session: Session = Depends(get_session)) -> ItemListRead:
    item_list = svc.get_list(session, list_id)
    return ItemListRead(
        **_summary_fields(item_list, svc.stats_of(session, item_list)),
        items=svc.item_values(session, item_list),
    )


@router.put("/{list_id}", response_model=ItemListWriteResult)
def update_list(
    list_id: str,
    payload: ItemListWrite,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ItemListWriteResult:
    """Replace the name, description and items. Flags using the list refresh on the next poll."""
    item_list = svc.get_changeable(session, list_id, user)
    report = svc.update_list(session, item_list, payload)
    return _write_result(session, item_list, report)


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(
    list_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Delete an unused list. A list a rule still reads is refused with LIST_IN_USE."""
    svc.delete_list(session, svc.get_changeable(session, list_id, user), user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _write_result(
    session: Session, item_list: ItemList, report: svc.SaveReport
) -> ItemListWriteResult:
    return ItemListWriteResult(
        **_summary_fields(item_list, svc.stats_of(session, item_list)),
        items=svc.item_values(session, item_list),
        received=report.received,
        kept=report.kept,
        duplicates_dropped=report.duplicates_dropped,
    )
