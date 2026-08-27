"""Admin account management: creating, editing, and resetting accounts.

This is the only place a ``User`` row is created or edited once the first
administrator exists - ``app/cli.py`` covers the two operations that need no
signed-in user at all (bootstrapping that first admin, and a shell-level
password reset for someone locked out with nobody left to help them). Every
other change to an account goes through here, from a router that has already
proven the caller is an administrator.

Every mutation writes its own audit entry through ``audit_service.record``.
Never construct ``AuditLog`` directly here or anywhere else - see that
module's docstring for why a second path around the credential scrub is
exactly the mistake an audit trail cannot afford.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.enums import AuditAction, UserRole
from app.models.user import User
from app.schemas.user import AuditEntryRead, UserCreate, UserUpdate
from app.security import passwords
from app.services import audit_service, session_service

#: How long an admin-issued temporary password stays usable, matching
#: ``app.cli.TEMP_PASSWORD_HOURS``. Duplicated rather than imported: importing
#: from ``app.cli`` would pull in Typer for a value the CLI and this service
#: merely happen to agree on, and the two are free to diverge if the reasons
#: for issuing a credential from a shell versus from this API ever differ.
#: Kept identical for now because there is no reason yet for a
#: console-issued and an admin-issued temporary password to expire on
#: different clocks.
TEMP_PASSWORD_HOURS = 72


def count_active_admins(db: Session) -> int:
    """How many admins could still administer this installation."""
    return db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    ) or 0


def guard_last_admin(
    db: Session, target: User, *, becoming_inactive: bool, becoming_analyst: bool
) -> None:
    """Refuse a change that would leave nobody able to administer.

    Enforced here rather than in the router because the router is not the only
    thing that will ever call this, and a rule that lives in one call site is a
    rule until somebody adds a second.
    """
    if not (becoming_inactive or becoming_analyst):
        return
    if target.role is not UserRole.ADMIN or not target.is_active:
        return
    if count_active_admins(db) > 1:
        return
    raise AppError(
        ErrorCode.LAST_ADMIN,
        "This is the only active administrator. Promote somebody else first.",
    )


def _get_or_404(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise AppError(ErrorCode.USER_NOT_FOUND, "No account with that id.")
    return user


def list_users(db: Session) -> list[User]:
    """Every account, newest first, for the admin account list."""
    return list(db.scalars(select(User).order_by(User.created_at.desc())))


def create_user(db: Session, actor: User, payload: UserCreate) -> tuple[User, str]:
    """Open an account with a system-generated password. Returns the user and
    the plaintext temporary password - the only place it exists outside the
    admin's own screen, and the caller must hand it over and then forget it,
    the same discipline ``session_service.issue`` uses for a session token.

    Raises ``DUPLICATE_EMAIL`` rather than letting the database's unique index
    and ``ck_users_email_lowercase`` CHECK constraint answer with a raw
    ``IntegrityError`` - that would surface as an unhandled 500 rather than a
    409 the frontend can branch on.
    """
    normalised = payload.email.strip().lower()

    existing = db.scalar(select(User).where(func.lower(User.email) == normalised))
    if existing is not None:
        raise AppError(
            ErrorCode.DUPLICATE_EMAIL, f"{normalised} already has an account."
        )

    temporary = passwords.generate_temporary_password()

    user = User(
        email=normalised,
        full_name=payload.full_name.strip(),
        password_hash=passwords.hash_password(temporary),
        role=payload.role,
        is_active=True,
        # An admin-issued credential nobody chose must be replaced, and must
        # not sit valid forever in whatever chat message carried it.
        must_change_password=True,
        temp_password_expires_at=utcnow() + timedelta(hours=TEMP_PASSWORD_HOURS),
        created_by=actor.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # No password anywhere in this detail, and scrub_detail would strip it
    # even if a future edit here got that wrong - see audit_log.py.
    audit_service.record(
        db,
        actor,
        AuditAction.USER_CREATED,
        target_type="user",
        target_id=user.id,
        detail={"email": user.email, "role": user.role.value},
    )
    return user, temporary


def update_user(db: Session, actor: User, user_id: str, payload: UserUpdate) -> User:
    """Deactivate/reactivate and change role, in one PATCH.

    ``guard_last_admin`` is checked against what the request is *asking for*,
    not against whether anything actually changes - re-sending
    ``{"is_active": false}`` on an already-inactive last admin must refuse the
    same as the first attempt, and the guard's own early return already
    handles "this account is not currently an active admin" for the case
    where the request would be a no-op anyway.
    """
    target = _get_or_404(db, user_id)

    guard_last_admin(
        db,
        target,
        becoming_inactive=payload.is_active is False,
        becoming_analyst=payload.role is UserRole.ANALYST,
    )

    changing_active = payload.is_active is not None and payload.is_active != target.is_active
    changing_role = payload.role is not None and payload.role != target.role
    previous_role = target.role

    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active

    db.commit()
    db.refresh(target)

    # Sessions are ended, and the audit trail written, only for the fields
    # that actually moved - sending {"is_active": true} at an already-active
    # account is not a reactivation and must not manufacture a log entry for
    # one.
    if changing_active:
        if not target.is_active:
            session_service.revoke_all_for_user(db, target.id)
            audit_service.record(
                db,
                actor,
                AuditAction.USER_DEACTIVATED,
                target_type="user",
                target_id=target.id,
                detail={"email": target.email},
            )
        else:
            audit_service.record(
                db,
                actor,
                AuditAction.USER_REACTIVATED,
                target_type="user",
                target_id=target.id,
                detail={"email": target.email},
            )

    if changing_role:
        audit_service.record(
            db,
            actor,
            AuditAction.USER_ROLE_CHANGED,
            target_type="user",
            target_id=target.id,
            detail={
                "email": target.email,
                "from_role": previous_role.value,
                "to_role": target.role.value,
            },
        )

    return target


def reset_password(db: Session, actor: User, user_id: str) -> str:
    """Issue a fresh temporary password for an account and end its sessions.

    A reset is issued exactly when an account is suspected compromised or its
    owner is locked out - either way, whoever currently holds a live session
    for it must not keep running alongside the new credential.
    """
    target = _get_or_404(db, user_id)

    temporary = passwords.generate_temporary_password()
    target.password_hash = passwords.hash_password(temporary)
    target.must_change_password = True
    target.temp_password_expires_at = utcnow() + timedelta(hours=TEMP_PASSWORD_HOURS)
    target.failed_login_count = 0
    target.locked_until = None
    db.commit()

    session_service.revoke_all_for_user(db, target.id)

    audit_service.record(
        db,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        target_type="user",
        target_id=target.id,
        detail={"email": target.email},
    )
    return temporary
