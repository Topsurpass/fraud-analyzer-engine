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
from app.models.enums import UserRole
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

#: Computed once, here, at import. The unknown-email branch below verifies
#: the caller's password against this instead of against a real user's hash,
#: so an unknown address costs the same one verify_password call as every
#: other failure branch. Hashing a fresh dummy value per request (the first
#: version of this function did that) is *two* argon2id operations against
#: every other branch's one -- a verify and the hash it was verifying
#: against -- which made an unknown address answer measurably *slower* than
#: a known one. That is the same timing oracle this constant exists to
#: close, just pointed the other way, and it was confirmed with a live
#: timing probe (see task-4-report.md) before and after this fix.
_DUMMY_HASH = passwords.hash_password("timing-equaliser")


def _is_last_active_admin(db: Session, user: User) -> bool:
    """Whether locking this account would leave nobody able to unlock it.

    A lockout is triggered by *failed* logins, so it needs no credential at
    all: anybody who knows an administrator's address can hold the console
    shut indefinitely at one request every ``LOCKOUT_MINUTES``, and recovery
    needs shell access. The design's "the last admin cannot be locked out"
    rule covers this the same way it covers deactivation and demotion - all
    three end with an installation nobody can administer.

    The exemption is narrow on purpose. It applies to exactly one account, and
    only while it is the sole active administrator; a second active admin
    means either can be locked because the other can still issue a reset. The
    IP rate limiter remains the bound on guessing at this one account, which
    is what it is for - a per-account lock does nothing against a distributed
    attempt anyway, and this account has to stay reachable.

    Inactive administrators are not counted: a deactivated account cannot
    reset anybody, so treating it as cover would lock out the only admin who
    can actually do anything.
    """
    if user.role is not UserRole.ADMIN or not user.is_active:
        return False
    others = db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
            User.id != user.id,
        )
    )
    return not others


def authenticate(db: Session, email: str, password: str) -> User:
    """The user behind these credentials, or raise.

    Raises ``ACCOUNT_LOCKED`` only for an account that is genuinely locked and
    whose password was otherwise correct-shaped; every other failure raises
    ``INVALID_CREDENTIALS``.
    """
    # Password first, lock second. See the comments at each branch below: the
    # order is what keeps a locked account indistinguishable from an unknown
    # one for anybody who does not already hold the password.
    now = utcnow()
    normalised = email.strip().lower()

    user = db.scalar(select(User).where(func.lower(User.email) == normalised))

    if user is None:
        # Verify anyway, against the precomputed dummy hash above. Returning
        # immediately makes an unknown address answer measurably faster than
        # a known one, which is the same oracle the shared message exists to
        # close -- and hashing a fresh dummy value here instead of reusing
        # one would overshoot it the other way; see _DUMMY_HASH's comment.
        passwords.verify_password(password, _DUMMY_HASH)
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    locked = user.locked_until is not None and user.locked_until > now

    # The password is verified *before* the lock is acted on, and that order is
    # the whole point. Refusing a locked account up front skipped argon2 and
    # answered ACCOUNT_LOCKED - a distinct status, code and message, and a
    # measurably faster one - but only for an address that exists. Six wrong
    # guesses therefore separated a registered address from an unregistered
    # one, which is precisely the oracle this module's docstring says is shut.
    if not passwords.verify_password(password, user.password_hash):
        # Not incremented while already locked. An attacker who can push the
        # lock forward with every wrong guess holds the account shut for as
        # long as they care to keep guessing, which turns a defence into a
        # weapon. A lock runs out on its own clock.
        if not locked:
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS and not _is_last_active_admin(
                db, user
            ):
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_login_count = 0
            db.commit()
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    # Only now, having proved they hold the password, is the caller told why
    # they are being refused. Someone who genuinely mistyped their way into a
    # lock finds out; a guesser gets _REFUSAL like everybody else.
    if locked:
        raise AppError(
            ErrorCode.ACCOUNT_LOCKED,
            f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.",
        )

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

    # Re-entering the same value is not a change, and under a forced change it
    # is the design failing completely: the admin-issued temporary password
    # becomes the permanent one, the admin permanently knows a working
    # credential, the value that travelled through a chat message stops being
    # temporary, and clearing temp_password_expires_at below cancels the
    # 72-hour expiry that bounded it.
    if passwords.verify_password(new, user.password_hash):
        raise AppError(
            ErrorCode.WEAK_PASSWORD,
            "Your new password must be different from your current one.",
        )

    passwords.validate_password_strength(new)

    user.password_hash = passwords.hash_password(new)
    user.must_change_password = False
    user.temp_password_expires_at = None
    db.commit()
