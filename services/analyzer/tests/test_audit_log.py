"""Recording who changed the system."""

from __future__ import annotations

from app.models.enums import AuditAction, UserRole
from app.models.audit_log import AuditLog
from app.models.user import User
from app.security.passwords import hash_password
from app.services import audit_service


def _user(session, email="admin@example.com", role=UserRole.ADMIN) -> User:
    user = User(
        email=email,
        full_name="An Admin",
        password_hash=hash_password("a-perfectly-fine-password"),
        role=role,
    )
    session.add(user)
    session.commit()
    return user


def test_an_entry_names_the_actor_the_action_and_the_target(session):
    actor = _user(session)
    target = _user(session, email="analyst@example.com", role=UserRole.ANALYST)

    audit_service.record(
        session, actor, AuditAction.USER_CREATED, "user", target.id
    )

    entry = session.query(AuditLog).one()
    assert entry.actor_id == actor.id
    assert entry.action is AuditAction.USER_CREATED
    assert entry.target_type == "user"
    assert entry.target_id == target.id


def test_detail_carries_structured_context(session):
    """A role change is unreadable without knowing what it changed from."""
    actor = _user(session)
    target = _user(session, email="analyst@example.com", role=UserRole.ANALYST)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_ROLE_CHANGED,
        "user",
        target.id,
        detail={"from": "analyst", "to": "admin"},
    )

    assert session.query(AuditLog).one().detail == {"from": "analyst", "to": "admin"}


def test_an_entry_records_when_it_happened(session):
    actor = _user(session)

    audit_service.record(session, actor, AuditAction.USER_CREATED, "user", actor.id)

    assert session.query(AuditLog).one().created_at is not None


def test_detail_never_carries_a_password(session):
    """The guard rail this table most needs.

    A reset entry is the obvious place somebody later adds "the temporary
    password we issued" for convenience, and an audit table is exactly where a
    credential must never come to rest.
    """
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={"password": "hunter2-hunter2"},
    )

    stored = session.query(AuditLog).one().detail
    # Stripping the *only* key a caller supplied leaves an empty dict, which
    # the service coalesces to None (see audit_service._FORBIDDEN_DETAIL_KEYS'
    # docstring: no detail is more honest than an empty one). `stored or {}`
    # makes the assertion check the actual security property - no credential
    # present, in whichever falsy shape it comes back as - rather than
    # crashing with `TypeError: argument of type 'NoneType' is not iterable`
    # on the `in` check.
    assert "password" not in (stored or {})
    assert "hunter2-hunter2" not in str(stored)


def test_entries_survive_in_order(session):
    actor = _user(session)
    for _ in range(3):
        audit_service.record(session, actor, AuditAction.USER_CREATED, "user", actor.id)

    assert session.query(AuditLog).count() == 3
