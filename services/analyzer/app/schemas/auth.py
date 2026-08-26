"""Request and response bodies for authentication.

``UserRead`` deliberately does not declare ``password_hash``. Credentials
cannot leak through a response model that has no field for them, which is the
same discipline ``ConnectionRead`` uses for target-database passwords.
"""

from __future__ import annotations

from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import UserRole
from app.schemas.types import UtcDatetime


def _validate_email(value: str) -> str:
    """Same rules as Pydantic's ``EmailStr``, with ``test_environment=True``.

    Plain ``EmailStr`` rejects any address under a reserved RFC 2606 TLD
    (``.test``, ``.invalid``, ``.localhost``, ...) as "special-use", which is
    exactly the TLD this suite's own fixtures use for test accounts
    (``analyst@b.test``) - that domain can never be a real mailbox, which is
    the point of reserving it, so refusing it here buys no real-world safety
    and would make every fixture in this file collide with a real registrar
    instead. ``check_deliverability`` stays off either way; nothing here does
    a DNS lookup.
    """
    try:
        return validate_email(value, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc


EmailAddress = Annotated[str, AfterValidator(_validate_email)]


class LoginRequest(BaseModel):
    email: EmailAddress
    # No length rule here. The policy applies to passwords being *set*, and
    # enforcing it on login would reject a valid old password after the policy
    # tightened, locking out the very people who complied with the old one.
    password: str = Field(min_length=1, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: UtcDatetime | None
    created_at: UtcDatetime


class LoginResponse(BaseModel):
    token: str
    user: UserRead


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)
