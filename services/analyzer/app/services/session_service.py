"""Issuing, resolving and revoking browser sessions.

The token is a random 256-bit value handed to the client once. Only its digest
is stored, so a leaked database gives an attacker hashes rather than live
sessions - the same reasoning as password storage, one layer up.

A fast digest rather than argon2: the token is already 256 bits of randomness,
so there is nothing to stretch, and running a memory-hard hash on every request
would be a denial of service this service performs on itself.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.user import User, UserSession

#: 32 bytes, url-safe. Long enough that guessing is not a strategy.
_TOKEN_BYTES = 32


def digest(raw_token: str) -> str:
    """The stored form of a token. Exposed for tests and for nothing else."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue(db: Session, user: User, ip: str | None, user_agent: str | None) -> str:
    """Open a session and return its token.

    The return value is the only time the raw token exists anywhere in this
    process; the caller must hand it to the client and forget it.
    """
    settings = get_settings()
    now = utcnow()
    raw = secrets.token_urlsafe(_TOKEN_BYTES)

    db.add(
        UserSession(
            id=digest(raw),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(hours=settings.session_absolute_hours),
            last_seen_at=now,
            ip=ip,
            # Truncated rather than rejected: a header this long is a curiosity,
            # not a reason to refuse somebody a login.
            user_agent=(user_agent or None) and user_agent[:400],
        )
    )
    db.commit()
    return raw


def resolve(db: Session, raw_token: str) -> User | None:
    """The live, active user behind a token, or None.

    Returns None for every failure - unknown, expired, idle, revoked, or
    belonging to a deactivated account - because the caller has exactly one
    response to all of them and distinguishing them would leak which.
    """
    if not raw_token:
        return None

    settings = get_settings()
    now = utcnow()

    stored = db.get(UserSession, digest(raw_token))
    if stored is None:
        return None

    if stored.expires_at <= now:
        return None
    if stored.last_seen_at + timedelta(hours=settings.session_idle_hours) <= now:
        return None

    user = db.get(User, stored.user_id)
    # Checked per request rather than trusted from the session row: this is the
    # whole reason sessions are server-side, and it is what makes deactivation
    # immediate.
    if user is None or not user.is_active:
        return None

    # Idle clock only. Extending the absolute expiry here would mean a session
    # in continuous use never ends, making the absolute lifetime decorative.
    stored.last_seen_at = now
    db.commit()
    return user


def revoke(db: Session, raw_token: str) -> None:
    """End one session. Silent if it was already gone.

    Logout must not report whether the token was real, and a logout that raises
    leaves somebody believing they are still signed in when they are not.
    """
    stored = db.get(UserSession, digest(raw_token))
    if stored is None:
        return
    db.delete(stored)
    db.commit()


def revoke_all_for_user(db: Session, user_id: str) -> int:
    """End every session for one account. Returns how many were ended.

    Used by deactivation, by a password change, and by a password reset - all
    three of which mean "whoever is holding a session for this account should
    stop holding it".
    """
    result = db.execute(delete(UserSession).where(UserSession.user_id == user_id))
    db.commit()
    return result.rowcount or 0


def purge_expired(db: Session) -> int:
    """Delete sessions past their absolute expiry. Returns how many.

    Called opportunistically on login rather than from a scheduler: a
    background job is one more thing that can stop running without anybody
    noticing, and this table only grows when people log in.
    """
    result = db.execute(delete(UserSession).where(UserSession.expires_at <= utcnow()))
    db.commit()
    return result.rowcount or 0


__all__ = ["digest", "issue", "purge_expired", "resolve", "revoke", "revoke_all_for_user"]
