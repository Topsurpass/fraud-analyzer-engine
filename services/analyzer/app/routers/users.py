"""Admin account management, and the audit trail it writes.

Two routers share this module rather than each getting its own file. They are
one feature from a caller's perspective - the audit log is the record of what
the account-management endpoints below just did - and every endpoint in both
routers needs exactly one guard, ``require_admin``, so there is no boundary
here worth a second file the way ``dashboards.py`` or ``flag_rules.py``
justify one by owning a distinct resource.

No endpoint here writes to ``AuditLog`` directly. ``app.services.user_service``
already calls ``audit_service.record`` for every mutation it makes, and that is
the only path an audit row is allowed to be born on - see that service
module's docstring for why a second path around the credential scrub is
exactly the mistake an audit trail cannot afford. This router's job is HTTP
plumbing: pull the caller's admin session, hand the request to the service,
and shape what comes back.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.auth import UserRead
from app.schemas.user import (
    AuditEntryRead,
    TemporaryPasswordResponse,
    UserCreate,
    UserCreateResponse,
    UserUpdate,
)
from app.security.deps import require_admin
from app.services import user_service as svc

router = APIRouter(
    prefix="/users",
    tags=["users"],
    # Router-level, not per-endpoint: an endpoint-level dependency is a thing
    # somebody has to remember on the day they add a route, and
    # tests/test_route_coverage.py fails the build the day they forget - see
    # app/security/deps.py's docstring for the same reasoning applied here.
    dependencies=[Depends(require_admin)],
)

#: Its own prefix rather than nested under /users: an audit entry can target a
#: connection or a saved query as easily as a user (see AuditLog.target_type),
#: so hanging this off /users would describe a scope the table does not have.
audit_router = APIRouter(
    prefix="/audit-log",
    tags=["users"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=list[UserRead])
def list_users(session: Session = Depends(get_session)) -> list[UserRead]:
    """Every account, newest first, for the admin account list."""
    return [UserRead.model_validate(user) for user in svc.list_users(session)]


@router.post("", response_model=UserCreateResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> UserCreateResponse:
    """Open an account with a system-generated password.

    The plaintext temporary password appears exactly once, in this response,
    and is never recoverable afterwards - not from a later GET on this
    account, and not from the audit entry ``user_service.create_user`` writes
    for it, which records only the email and role.
    """
    user, temporary_password = svc.create_user(session, admin, payload)
    return UserCreateResponse(
        user=UserRead.model_validate(user), temporary_password=temporary_password
    )


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: str,
    payload: UserUpdate,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> UserRead:
    """Deactivate/reactivate an account, change its role, or both at once.

    Refused with ``LAST_ADMIN`` (409) if the change would leave the
    installation with no active administrator - see
    ``user_service.guard_last_admin``. Refused with ``USER_NOT_FOUND`` (404)
    if ``user_id`` does not name an existing account.
    """
    updated = svc.update_user(session, admin, user_id, payload)
    return UserRead.model_validate(updated)


@router.post("/{user_id}/reset-password", response_model=TemporaryPasswordResponse)
def reset_password(
    user_id: str,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TemporaryPasswordResponse:
    """Issue a fresh temporary password and end every live session on this
    account at once.

    A reset is issued exactly when an account is suspected compromised or its
    owner is locked out; either way, the person currently holding the old
    credential must not keep running alongside the new one.
    """
    temporary_password = svc.reset_password(session, admin, user_id)
    return TemporaryPasswordResponse(temporary_password=temporary_password)


@audit_router.get("", response_model=list[AuditEntryRead])
def list_audit_log(session: Session = Depends(get_session)) -> list[AuditEntryRead]:
    """Every audit entry, newest first, with the actor's email resolved.

    The table stores ``actor_id`` rather than an email because accounts are
    never deleted and an id is what survives an email changing (see
    ``AuditEntryRead``'s docstring), but a screen rendering the log for a
    human needs something readable without a second lookup per row - hence
    the join here rather than in the schema or the model.
    """
    rows = session.execute(
        select(AuditLog, User.email)
        .join(User, User.id == AuditLog.actor_id)
        .order_by(AuditLog.created_at.desc())
    ).all()
    return [
        AuditEntryRead(
            id=entry.id,
            actor_id=entry.actor_id,
            actor_email=email,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            detail=entry.detail,
            created_at=entry.created_at,
        )
        for entry, email in rows
    ]
