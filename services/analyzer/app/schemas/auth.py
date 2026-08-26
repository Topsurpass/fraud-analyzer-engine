"""Request and response bodies for authentication.

``UserRead`` deliberately does not declare ``password_hash``. Credentials
cannot leak through a response model that has no field for them, which is the
same discipline ``ConnectionRead`` uses for target-database passwords.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import UserRole
from app.schemas.types import UtcDatetime


class LoginRequest(BaseModel):
    email: EmailStr
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
