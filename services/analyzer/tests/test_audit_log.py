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
    """Three insert calls land with timestamps that never run backward.

    Asserted against the objects ``record`` itself returns, in call order --
    not against a query re-sorted by the database -- because the property
    under test is "did the writes happen in order", and re-deriving order
    from a query would only prove the query's ORDER BY works, not that the
    writes themselves were sequential.
    """
    actor = _user(session)
    written = [
        audit_service.record(session, actor, AuditAction.USER_CREATED, "user", actor.id)
        for _ in range(3)
    ]

    assert len(written) == 3
    assert all(
        written[i].created_at <= written[i + 1].created_at
        for i in range(len(written) - 1)
    )
    assert session.query(AuditLog).count() == 3


def test_detail_never_carries_a_capitalised_password_key(session):
    """A second author is at least as likely to write ``Password`` as
    ``password``. Matching case-sensitively would let that variant straight
    through, which is worse than not filtering at all -- it looks safe."""
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={"Password": "hunter2-hunter2"},
    )

    stored = session.query(AuditLog).one().detail
    assert "Password" not in (stored or {})
    assert "hunter2-hunter2" not in str(stored)


def test_detail_never_carries_an_upper_case_password_key(session):
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={"PASSWORD": "hunter2-hunter2"},
    )

    stored = session.query(AuditLog).one().detail
    assert "PASSWORD" not in (stored or {})
    assert "hunter2-hunter2" not in str(stored)


def test_detail_never_carries_a_nested_password(session):
    """A caller who reaches for ``{"user": {"password": ...}}`` -- describing
    the account a reset touched, with the credential riding along inside it
    -- is an ordinary shape for "what changed", not a contrived attack."""
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={"user": {"email": "analyst@example.com", "password": "hunter2-hunter2"}},
    )

    stored = session.query(AuditLog).one().detail
    assert "hunter2-hunter2" not in str(stored)
    assert "password" not in stored["user"]
    # The rest of the nested structure survives; only the credential is cut.
    assert stored["user"]["email"] == "analyst@example.com"


def test_detail_never_carries_a_password_nested_in_a_list(session):
    """The same nesting problem, one shape further: a batch operation
    describing several accounts as a list of records."""
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={
            "users": [
                {"email": "a@example.com", "password": "hunter2-hunter2"},
                {"email": "b@example.com", "password": "hunter3-hunter3"},
            ]
        },
    )

    stored = session.query(AuditLog).one().detail
    assert "hunter2-hunter2" not in str(stored)
    assert "hunter3-hunter3" not in str(stored)
    for record_ in stored["users"]:
        assert "password" not in record_
    assert [r["email"] for r in stored["users"]] == ["a@example.com", "b@example.com"]


def test_detail_never_carries_a_whitespace_padded_password_key(session):
    """``key.lower()`` alone does not catch `"  PaSsWoRd  "` -- a key with
    incidental leading/trailing whitespace from copy-pasted JSON or a
    templated payload is still exactly the same credential under a
    cosmetically different name, and stripping only case would leave it
    through."""
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={"  PaSsWoRd  ": "hunter2-hunter2"},
    )

    stored = session.query(AuditLog).one().detail
    assert not stored or all("password" not in k.strip().lower() for k in stored)
    assert "hunter2-hunter2" not in str(stored)


def test_detail_never_carries_a_temp_password(session):
    """The brief's own motivating scenario, by its most likely name.

    ``User.temp_password_expires_at`` and ``generate_temporary_password()``
    already name this concept elsewhere in the codebase, which makes
    ``temp_password`` the single most likely key a future call site reaches
    for when it wants to record "the temporary password we issued" -- the
    exact case the brief calls out and the original filter missed entirely.
    """
    actor = _user(session)

    audit_service.record(
        session,
        actor,
        AuditAction.USER_PASSWORD_RESET,
        "user",
        actor.id,
        detail={"temp_password": "hunter2-hunter2"},
    )

    stored = session.query(AuditLog).one().detail
    assert "temp_password" not in (stored or {})
    assert "hunter2-hunter2" not in str(stored)


def test_the_model_scrubs_detail_even_when_constructed_directly(session):
    """The filter must hold as a property of the table, not a convention one
    call site can skip.

    Task 2 adds several call sites; a caller that builds ``AuditLog`` by hand
    instead of going through ``audit_service.record`` -- accidentally or as a
    shortcut -- must not be able to write a credential just by skipping the
    service function. ``AuditLog.detail``'s ``@validates`` hook is what makes
    that true regardless of which code path constructed the row.
    """
    actor = _user(session)

    entry = AuditLog(
        actor_id=actor.id,
        action=AuditAction.USER_PASSWORD_RESET,
        target_type="user",
        target_id=actor.id,
        detail={"password": "hunter2-hunter2"},
    )
    session.add(entry)
    session.commit()

    stored = session.query(AuditLog).one().detail
    assert "password" not in (stored or {})
    assert "hunter2-hunter2" not in str(stored)
