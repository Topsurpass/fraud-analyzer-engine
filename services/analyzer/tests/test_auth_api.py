"""Logging in, out, and who am I."""

from __future__ import annotations

from datetime import timedelta

from app.db.app_state import get_sessionmaker
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User, UserSession
from app.security.passwords import hash_password
from app.services import session_service
from app.services.auth_service import MAX_FAILED_LOGINS

PASSWORD = "a-perfectly-fine-password"


def make_user(**over) -> User:
    """Insert a user directly. There is no admin API yet to create one with."""
    db = get_sessionmaker()()
    try:
        user = User(
            email=over.pop("email", "analyst@example.com"),
            full_name=over.pop("full_name", "An Analyst"),
            password_hash=hash_password(over.pop("password", PASSWORD)),
            role=over.pop("role", UserRole.ANALYST),
            **over,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def login(client, email="analyst@example.com", password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_a_correct_password_returns_a_token_and_the_user(client, app_db):
    make_user()

    response = login(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"]
    assert body["user"]["email"] == "analyst@example.com"
    assert body["user"]["role"] == "analyst"


def test_the_password_hash_never_appears_in_a_response(client, app_db):
    make_user()

    body = login(client).json()

    assert "password_hash" not in body["user"]
    assert "password" not in body["user"]


def test_email_is_matched_without_regard_to_case(client, app_db):
    """Somebody typing their address with a capital is not a failed login."""
    make_user(email="analyst@example.com")

    assert login(client, email="Analyst@Example.com").status_code == 200


def test_a_wrong_password_is_refused(client, app_db):
    make_user()

    response = login(client, password="wrong-but-long-enough")

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


def test_an_unknown_email_gives_the_same_answer_as_a_wrong_password(client, app_db):
    """Otherwise the login form is an oracle for which addresses have accounts,
    which is the first thing an attacker enumerates."""
    make_user()

    unknown = login(client, email="nobody@example.com")
    wrong = login(client, password="wrong-but-long-enough")

    assert unknown.status_code == wrong.status_code
    assert unknown.json()["error_code"] == wrong.json()["error_code"]
    assert unknown.json()["message"] == wrong.json()["message"]


def test_a_deactivated_account_cannot_log_in(client, app_db):
    make_user(is_active=False)

    response = login(client)

    assert response.status_code == 401
    # Same answer again: "that account is switched off" tells an attacker the
    # address is real.
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


def test_repeated_failures_lock_the_account(client, app_db):
    make_user()

    for _ in range(5):
        login(client, password="wrong-but-long-enough")

    response = login(client)
    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCOUNT_LOCKED"


def test_a_successful_login_clears_the_failure_count(client, app_db):
    make_user()
    login(client, password="wrong-but-long-enough")
    login(client, password="wrong-but-long-enough")

    assert login(client).status_code == 200

    for _ in range(3):
        login(client, password="wrong-but-long-enough")
    # Three more failures must not lock, because the count restarted.
    assert login(client).status_code == 200


def test_a_lock_releases_once_it_expires(client, app_db):
    user = make_user()
    for _ in range(5):
        login(client, password="wrong-but-long-enough")

    db = get_sessionmaker()()
    try:
        stored = db.get(User, user.id)
        stored.locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert login(client).status_code == 200


def test_a_login_records_when_it_happened(client, app_db):
    user = make_user()

    login(client)

    db = get_sessionmaker()()
    try:
        assert db.get(User, user.id).last_login_at is not None
    finally:
        db.close()


def test_me_returns_the_signed_in_user(client, app_db):
    make_user()
    token = login(client).json()["token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "analyst@example.com"


def test_me_without_a_token_is_refused(client, app_db):
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_me_with_a_nonsense_token_is_refused(client, app_db):
    response = client.get("/auth/me", headers={"Authorization": "Bearer nonsense"})

    assert response.status_code == 401


def test_logging_out_ends_the_session(client, app_db):
    make_user()
    token = login(client).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert client.post("/auth/logout", headers=auth).status_code == 204
    assert client.get("/auth/me", headers=auth).status_code == 401


def test_changing_a_password_lets_the_new_one_log_in(client, app_db):
    make_user()
    token = login(client).json()["token"]

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": "another-fine-password"},
    )

    assert response.status_code == 204
    assert login(client, password="another-fine-password").status_code == 200


def test_changing_a_password_requires_the_current_one(client, app_db):
    """A borrowed unlocked laptop must not become a stolen account."""
    make_user()
    token = login(client).json()["token"]

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "not-the-password", "new_password": "another-fine-password"},
    )

    assert response.status_code == 401


def test_changing_a_password_refuses_a_weak_one(client, app_db):
    make_user()
    token = login(client).json()["token"]

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": "short"},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "WEAK_PASSWORD"


def test_changing_a_password_ends_every_other_session(client, app_db):
    """If the old password was intercepted and used, changing it must eject
    whoever is holding that session rather than run alongside them."""
    make_user()
    stolen = login(client).json()["token"]
    mine = login(client).json()["token"]

    client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {mine}"},
        json={"current_password": PASSWORD, "new_password": "another-fine-password"},
    )

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {stolen}"}).status_code == 401
    # The session that performed the change keeps working; being signed out of
    # the browser you just used is a bug, not a security feature.
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {mine}"}).status_code == 200


def test_changing_a_password_keeps_the_survivors_original_absolute_expiry(client, app_db):
    """A password change proves the current password; it is not a fresh
    login. Restoring the session with a brand new session_absolute_hours
    window would let anyone dodge the absolute cap forever just by changing
    their password on a schedule, so the surviving row must keep the expiry
    it was issued with."""
    make_user()
    token = login(client).json()["token"]

    db = get_sessionmaker()()
    try:
        before = db.get(UserSession, session_service.digest(token))
        original_created_at = before.created_at
        original_expires_at = before.expires_at
    finally:
        db.close()

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": "another-fine-password"},
    )
    assert response.status_code == 204

    db = get_sessionmaker()()
    try:
        after = db.get(UserSession, session_service.digest(token))
        assert after is not None
        assert after.created_at == original_created_at
        assert after.expires_at == original_expires_at
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The lockout must not become an oracle, and must not become a weapon
# ---------------------------------------------------------------------------


def test_a_locked_account_answers_exactly_like_an_unknown_one(client, app_db):
    """The lockout used to be an email-existence oracle.

    ``ACCOUNT_LOCKED`` was raised before the password was verified and only for
    an address that exists, so six wrong guesses told an attacker whether an
    address was registered - by status code *and* by message - which is the
    enumeration hole the module docstring and the design both promise is shut.
    A wrong password must look the same whether the account is locked, unlocked
    or absent.
    """
    make_user(email="locked@example.com")
    for _ in range(MAX_FAILED_LOGINS):
        login(client, email="locked@example.com", password="wrong-but-long-enough")

    known = login(client, email="locked@example.com", password="still-wrong-and-long")
    unknown = login(client, email="nobody@example.com", password="still-wrong-and-long")

    assert known.status_code == unknown.status_code
    assert known.json()["error_code"] == unknown.json()["error_code"]
    assert known.json()["message"] == unknown.json()["message"]


def test_the_person_who_mistyped_still_learns_the_account_is_locked(client, app_db):
    """The other half of the same rule: someone who submits the *correct*
    password has proved they are the account holder, so telling them why they
    are being refused costs nothing and saves a support ticket."""
    make_user()
    for _ in range(MAX_FAILED_LOGINS):
        login(client, password="wrong-but-long-enough")

    response = login(client)

    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCOUNT_LOCKED"


def test_failed_attempts_during_a_lock_do_not_extend_it(client, app_db):
    """Otherwise an attacker keeps an account locked forever by guessing at it.

    A guard on the fix rather than a reproduction of the old bug: the old code
    returned before the counter, so it did not extend either. Verifying the
    password first - which is what closes the oracle above - is exactly what
    would reintroduce this if the counter were left unguarded.
    """
    user = make_user()
    for _ in range(MAX_FAILED_LOGINS):
        login(client, password="wrong-but-long-enough")

    db = get_sessionmaker()()
    try:
        locked_until = db.get(User, user.id).locked_until
        assert locked_until is not None
    finally:
        db.close()

    for _ in range(MAX_FAILED_LOGINS):
        login(client, password="wrong-but-long-enough")

    db = get_sessionmaker()()
    try:
        stored = db.get(User, user.id)
        assert stored.locked_until == locked_until
        assert stored.failed_login_count == 0
    finally:
        db.close()


def test_the_last_active_admin_cannot_be_locked_out(client, app_db):
    """"The last admin cannot be locked out" is a design rule, and a lockout
    triggered by *failed* logins needs no credential at all - so anyone knowing
    an admin's address could hold the fraud console shut indefinitely, with
    recovery needing shell access. The IP rate limiter remains the bound on
    guessing this account."""
    make_user(email="boss@example.com", role=UserRole.ADMIN)

    for _ in range(MAX_FAILED_LOGINS * 2):
        login(client, email="boss@example.com", password="wrong-but-long-enough")

    response = login(client, email="boss@example.com")

    assert response.status_code == 200, response.text

    db = get_sessionmaker()()
    try:
        assert db.get(User, response.json()["user"]["id"]).locked_until is None
    finally:
        db.close()


def test_an_admin_with_a_colleague_still_locks(client, app_db):
    """The exemption is only for the *last* one. Two active admins means
    locking either still leaves somebody able to unlock the other."""
    make_user(email="boss@example.com", role=UserRole.ADMIN)
    make_user(email="deputy@example.com", role=UserRole.ADMIN)

    for _ in range(MAX_FAILED_LOGINS):
        login(client, email="boss@example.com", password="wrong-but-long-enough")

    response = login(client, email="boss@example.com")

    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCOUNT_LOCKED"


def test_an_inactive_admin_does_not_count_as_the_last_one(client, app_db):
    """A deactivated admin cannot unlock anybody, so it must not be the reason
    the only *active* admin loses the exemption."""
    make_user(email="boss@example.com", role=UserRole.ADMIN)
    make_user(email="retired@example.com", role=UserRole.ADMIN, is_active=False)

    for _ in range(MAX_FAILED_LOGINS * 2):
        login(client, email="boss@example.com", password="wrong-but-long-enough")

    assert login(client, email="boss@example.com").status_code == 200


# ---------------------------------------------------------------------------
# A forced password change must actually change the password
# ---------------------------------------------------------------------------


def test_the_new_password_cannot_be_the_current_one(client, app_db):
    """Re-entering the admin-issued temporary password satisfied the forced
    change: the flag cleared, the 72-hour expiry was wiped, and the credential
    that travelled through a chat message became the permanent one - which the
    admin still knows."""
    user = make_user(
        must_change_password=True,
        temp_password_expires_at=utcnow() + timedelta(hours=72),
    )
    token = login(client).json()["token"]

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": PASSWORD},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "WEAK_PASSWORD"

    db = get_sessionmaker()()
    try:
        stored = db.get(User, user.id)
        assert stored.must_change_password is True
        assert stored.temp_password_expires_at is not None
    finally:
        db.close()


def test_a_password_change_keeps_the_surviving_sessions_provenance(client, app_db):
    """``issue_with_id`` restored the surviving row without ``ip`` or
    ``user_agent``, so every password change quietly erased where that session
    came from - the one field that answers "was this session opened from
    somewhere I recognise" after a suspected compromise."""
    make_user()
    token = login(
        client, email="analyst@example.com"
    ).json()["token"]

    db = get_sessionmaker()()
    try:
        before = db.get(UserSession, session_service.digest(token))
        original_ip, original_agent = before.ip, before.user_agent
        assert original_agent
    finally:
        db.close()

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": "another-fine-password"},
    )
    assert response.status_code == 204

    db = get_sessionmaker()()
    try:
        after = db.get(UserSession, session_service.digest(token))
        assert after.ip == original_ip
        assert after.user_agent == original_agent
    finally:
        db.close()


def test_a_promoted_colleague_does_not_lock_the_admin_who_was_exempt(client, app_db):
    """The exemption has to leave no debt behind it.

    Skipping only the *lock* while exempt let ``failed_login_count`` free-run:
    it climbed past MAX_FAILED_LOGINS and was cleared by nothing but a
    successful login. The moment a second active admin appeared the exemption
    lifted with the count already over the threshold, so the founding admin's
    next mistype locked them out instantly - triggered by onboarding a
    colleague, with nothing visible connecting cause to effect. The increment
    itself is skipped now, the same way it is skipped while a lock is live.
    """
    boss = make_user(email="boss@example.com", role=UserRole.ADMIN)
    for _ in range(MAX_FAILED_LOGINS):
        login(client, email="boss@example.com", password="wrong-but-long-enough")

    # Onboarding a colleague lifts the exemption.
    make_user(email="deputy@example.com", role=UserRole.ADMIN)
    login(client, email="boss@example.com", password="wrong-but-long-enough")

    response = login(client, email="boss@example.com")
    assert response.status_code == 200, response.text

    # And the reason it holds: no debt was carried out of the exempt period.
    db = get_sessionmaker()()
    try:
        stored = db.get(User, boss.id)
        assert stored.locked_until is None
        assert stored.failed_login_count == 0
    finally:
        db.close()
