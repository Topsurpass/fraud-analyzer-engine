"""Issuing, resolving and revoking sessions."""

from __future__ import annotations

from datetime import timedelta

from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User, UserSession
from app.security.passwords import hash_password
from app.services import session_service


def _user(session, **over) -> User:
    user = User(
        email=over.pop("email", "a@b.test"),
        full_name="A",
        password_hash=hash_password("a-perfectly-fine-password"),
        role=over.pop("role", UserRole.ANALYST),
        **over,
    )
    session.add(user)
    session.commit()
    return user


def test_a_fresh_token_resolves_to_its_user(session):
    user = _user(session)
    token = session_service.issue(session, user, ip="127.0.0.1", user_agent="pytest")

    assert session_service.resolve(session, token) is not None
    assert session_service.resolve(session, token).id == user.id


def test_the_raw_token_is_never_stored(session):
    """A database dump must not hand over live sessions."""
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    stored = session.query(UserSession).one()
    assert stored.id != token
    assert token not in stored.id


def test_an_unknown_token_resolves_to_nobody(session):
    _user(session)
    assert session_service.resolve(session, "not-a-real-token") is None


def test_a_revoked_token_stops_working(session):
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    session_service.revoke(session, token)

    assert session_service.resolve(session, token) is None


def test_revoking_an_unknown_token_is_silent(session):
    """Logout must not report whether the token was real - and a logout that
    500s leaves the user believing they are still signed in."""
    session_service.revoke(session, "not-a-real-token")


def test_a_deactivated_user_cannot_resolve_a_live_session(session):
    """The reason sessions are server-side at all. A token issued before
    deactivation must stop working the instant the flag flips, not whenever it
    would have expired."""
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    user.is_active = False
    session.commit()

    assert session_service.resolve(session, token) is None


def test_an_expired_session_does_not_resolve(session):
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    stored = session.query(UserSession).one()
    stored.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    assert session_service.resolve(session, token) is None


def test_an_idle_session_does_not_resolve(session):
    """Separate from absolute expiry: a laptop left open in a shared office is
    the case this covers."""
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    stored = session.query(UserSession).one()
    stored.last_seen_at = utcnow() - timedelta(days=30)
    session.commit()

    assert session_service.resolve(session, token) is None


def test_using_a_session_moves_its_idle_clock(session):
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    stored = session.query(UserSession).one()
    stored.last_seen_at = utcnow() - timedelta(hours=1)
    session.commit()
    before = stored.last_seen_at

    session_service.resolve(session, token)

    session.refresh(stored)
    assert stored.last_seen_at > before


def test_using_a_session_does_not_extend_its_absolute_expiry(session):
    """Otherwise a session that is used continuously never ends, and the
    absolute lifetime is decorative."""
    user = _user(session)
    token = session_service.issue(session, user, ip=None, user_agent=None)

    stored = session.query(UserSession).one()
    before = stored.expires_at

    session_service.resolve(session, token)

    session.refresh(stored)
    assert stored.expires_at == before


def test_revoking_every_session_signs_the_user_out_everywhere(session):
    user = _user(session)
    first = session_service.issue(session, user, ip=None, user_agent=None)
    second = session_service.issue(session, user, ip=None, user_agent=None)

    assert session_service.revoke_all_for_user(session, user.id) == 2
    assert session_service.resolve(session, first) is None
    assert session_service.resolve(session, second) is None


def test_revoking_one_users_sessions_leaves_another_alone(session):
    one = _user(session, email="one@b.test")
    two = _user(session, email="two@b.test")
    kept = session_service.issue(session, two, ip=None, user_agent=None)
    session_service.issue(session, one, ip=None, user_agent=None)

    session_service.revoke_all_for_user(session, one.id)

    assert session_service.resolve(session, kept) is not None


def test_purging_removes_only_expired_rows(session):
    user = _user(session)
    live = session_service.issue(session, user, ip=None, user_agent=None)
    session_service.issue(session, user, ip=None, user_agent=None)

    dead = (
        session.query(UserSession)
        .filter(UserSession.id != session_service.digest(live))
        .one()
    )
    dead.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    assert session_service.purge_expired(session) == 1
    assert session_service.resolve(session, live) is not None
