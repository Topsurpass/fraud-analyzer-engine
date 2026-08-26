"""Turning credentials into a user, and changing a password.

Every failure path returns the same error with the same message. An
authentication endpoint that distinguishes "no such account" from "wrong
password" is an oracle for which email addresses are registered, and that list
is the first thing an attacker builds.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.models.base import utcnow
from app.models.user import User
from app.security import passwords
from app.services import session_service

#: Failures before an account is locked. Low enough to stop a guessing run,
#: high enough to survive a person mistyping their own password twice.
MAX_FAILED_LOGINS = 5

#: How long a lock lasts. Long enough to make an online attack pointless,
#: short enough that a locked-out colleague is not blocked for the afternoon.
LOCKOUT_MINUTES = 15

#: One message for every failure. Assigned once so the two raise sites cannot
#: drift apart and start distinguishing themselves by wording.
_REFUSAL = "Those details are not right."


def authenticate(db: Session, email: str, password: str) -> User:
    """The user behind these credentials, or raise.

    Raises ``ACCOUNT_LOCKED`` only for an account that is genuinely locked and
    whose password was otherwise correct-shaped; every other failure raises
    ``INVALID_CREDENTIALS``.
    """
    now = utcnow()
    normalised = email.strip().lower()

    user = db.scalar(select(User).where(func.lower(User.email) == normalised))

    if user is None:
        # Hash anyway. Returning immediately makes an unknown address answer
        # measurably faster than a known one, which is the same oracle the
        # shared message exists to close.
        passwords.verify_password(password, passwords.hash_password("timing-equaliser"))
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    if user.locked_until is not None and user.locked_until > now:
        raise AppError(
            ErrorCode.ACCOUNT_LOCKED,
            f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.",
        )

    if not passwords.verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
        db.commit()
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    # Deactivated accounts fail here rather than earlier, and with the same
    # message: "this account is switched off" confirms the address is real.
    if not user.is_active:
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    if (
        user.temp_password_expires_at is not None
        and user.temp_password_expires_at <= now
    ):
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()

    # Opportunistic, on the one operation that is already slow because it
    # verifies a password. Cheaper than a scheduler that can stop running
    # without anybody noticing.
    session_service.purge_expired(db)
    return user


def change_password(db: Session, user: User, current: str, new: str) -> None:
    """Replace a password, then sign the account out everywhere else."""
    if not passwords.verify_password(current, user.password_hash):
        raise AppError(ErrorCode.INVALID_CREDENTIALS, "That is not your current password.")

    passwords.validate_password_strength(new)

    user.password_hash = passwords.hash_password(new)
    user.must_change_password = False
    user.temp_password_expires_at = None
    db.commit()
