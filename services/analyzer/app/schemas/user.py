"""Request and response bodies for admin account management.

``UserRead`` is deliberately not redefined here. ``app/schemas/auth.py``
already owns the one shape a user renders as, and a second definition would
drift the moment somebody added a field to one and not the other - the
temporary-password endpoints below reuse it rather than compete with it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import AuditAction, UserRole
from app.schemas.auth import UserRead
from app.schemas.types import UtcDatetime


class UserCreate(BaseModel):
    """What an admin supplies to open an account.

    No ``password`` field exists here, on purpose - not "optional and
    ignored", but structurally absent. There is nowhere in this schema for an
    admin to supply one, which is what makes ``test_the_admin_never_chooses_the_password``
    true by construction rather than by a check somewhere in a service
    function that a later refactor could drop.
    """

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: UserRole


class UserUpdate(BaseModel):
    """A partial edit: deactivate/reactivate, change role, or both at once.

    Both fields optional and default to ``None`` so a PATCH that sends only
    ``{"is_active": false}`` leaves the role untouched, and vice versa. ``None``
    is never itself a value either field can hold in the database, so "not
    supplied" and "set to null" cannot be confused.
    """

    is_active: bool | None = None
    role: UserRole | None = None


class UserCreateResponse(BaseModel):
    """The one and only place a freshly issued temporary password appears."""

    user: UserRead
    temporary_password: str


class TemporaryPasswordResponse(BaseModel):
    """The one and only place a reset's temporary password appears."""

    temporary_password: str


class AuditEntryRead(BaseModel):
    """One row of the audit log, with the actor's email resolved for display.

    Mirrors ``app.models.audit_log.AuditLog`` field for field, plus
    ``actor_email`` - the log stores ``actor_id`` because accounts are never
    deleted and an id is what survives an email changing, but a page rendering
    the log for a human needs something readable without a second lookup per
    row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str
    actor_email: str
    action: AuditAction
    target_type: str
    target_id: str
    detail: dict | None
    created_at: UtcDatetime
