"""Logging in, out, and who am I."""

from __future__ import annotations

from datetime import timedelta

from app.db.app_state import get_sessionmaker
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User, UserSession
from app.security.passwords import hash_password
from app.services import session_service

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
