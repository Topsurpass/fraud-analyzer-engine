"""Admin account management."""

from __future__ import annotations

from app.db.app_state import get_sessionmaker
from app.models.enums import AuditAction, UserRole
from app.models.audit_log import AuditLog
from app.models.user import User
from tests.test_auth_api import login, make_user

PASSWORD = "a-perfectly-fine-password"


def _auth(client, email, role):
    make_user(email=email, role=role)
    return {"Authorization": f"Bearer {login(client, email=email).json()['token']}"}


def admin_auth(client):
    return _auth(client, "boss@example.com", UserRole.ADMIN)


def analyst_auth(client):
    return _auth(client, "analyst@example.com", UserRole.ANALYST)


def test_an_admin_lists_every_account(client, app_db):
    auth = admin_auth(client)
    make_user(email="kemi@example.com", role=UserRole.ANALYST)

    response = client.get("/users", headers=auth)

    assert response.status_code == 200
    assert {u["email"] for u in response.json()} == {"boss@example.com", "kemi@example.com"}


def test_an_analyst_cannot_list_accounts(client, app_db):
    auth = analyst_auth(client)

    assert client.get("/users", headers=auth).status_code == 403


def test_creating_an_account_returns_a_temporary_password_once(client, app_db):
    auth = admin_auth(client)

    response = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["temporary_password"]
    assert body["user"]["must_change_password"] is True
    assert body["user"]["email"] == "kemi@example.com"


def test_the_admin_never_chooses_the_password(client, app_db):
    """A human-chosen temporary password is reliably weak and reliably reused
    across every account that person creates."""
    auth = admin_auth(client)

    response = client.post(
        "/users",
        headers=auth,
        json={
            "email": "kemi@example.com",
            "full_name": "Kemi",
            "role": "analyst",
            "password": "chosen-by-the-admin",
        },
    )

    # The field is not in the schema at all, so it is ignored rather than honoured.
    assert response.status_code == 201
    assert response.json()["temporary_password"] != "chosen-by-the-admin"
    # The differing string alone would not catch a future refactor that
    # quietly stashed the supplied value as a second, valid credential -
    # only actually trying to log in with it closes that gap.
    assert (
        login(client, email="kemi@example.com", password="chosen-by-the-admin").status_code
        == 401
    )


def test_the_temporary_password_actually_logs_in(client, app_db):
    auth = admin_auth(client)
    body = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()

    assert login(client, email="kemi@example.com", password=body["temporary_password"]).status_code == 200


def test_the_hash_never_appears_in_any_response(client, app_db):
    auth = admin_auth(client)
    body = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()

    assert "password_hash" not in body["user"]
    assert "password_hash" not in client.get("/users", headers=auth).text


def test_a_duplicate_email_is_refused(client, app_db):
    auth = admin_auth(client)
    payload = {"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"}
    client.post("/users", headers=auth, json=payload)

    response = client.post("/users", headers=auth, json=payload)

    assert response.status_code == 409
    assert response.json()["error_code"] == "DUPLICATE_EMAIL"


def test_an_email_is_normalised_before_it_is_stored(client, app_db):
    """users.email has a CHECK constraint requiring lowercase, so a mixed-case
    address would be refused by the database rather than by us."""
    auth = admin_auth(client)

    response = client.post(
        "/users",
        headers=auth,
        json={"email": "Kemi@Example.COM", "full_name": "Kemi", "role": "analyst"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["user"]["email"] == "kemi@example.com"


def test_deactivating_an_account_blocks_its_login(client, app_db):
    auth = admin_auth(client)
    created = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()

    response = client.patch(
        f"/users/{created['user']['id']}", headers=auth, json={"is_active": False}
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert login(client, email="kemi@example.com", password=created["temporary_password"]).status_code == 401


def test_deactivating_an_account_kills_its_live_sessions(client, app_db):
    """The reason sessions are server-side. Deactivation that waits for a token
    to expire is not deactivation."""
    auth = admin_auth(client)
    created = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()
    kemi_token = login(
        client, email="kemi@example.com", password=created["temporary_password"]
    ).json()["token"]

    client.patch(f"/users/{created['user']['id']}", headers=auth, json={"is_active": False})

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {kemi_token}"}).status_code == 401


def test_the_last_active_admin_cannot_be_deactivated(client, app_db):
    """One mis-click while tidying an account list would otherwise leave an
    installation nobody can administer, recoverable only by shell access."""
    auth = admin_auth(client)
    me = client.get("/auth/me", headers=auth).json()

    response = client.patch(f"/users/{me['id']}", headers=auth, json={"is_active": False})

    assert response.status_code == 409
    assert response.json()["error_code"] == "LAST_ADMIN"


def test_the_last_active_admin_cannot_be_demoted(client, app_db):
    auth = admin_auth(client)
    me = client.get("/auth/me", headers=auth).json()

    response = client.patch(f"/users/{me['id']}", headers=auth, json={"role": "analyst"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "LAST_ADMIN"


def test_an_admin_can_be_demoted_once_another_exists(client, app_db):
    auth = admin_auth(client)
    me = client.get("/auth/me", headers=auth).json()
    client.post(
        "/users",
        headers=auth,
        json={"email": "second@example.com", "full_name": "Second", "role": "admin"},
    )

    assert client.patch(f"/users/{me['id']}", headers=auth, json={"role": "analyst"}).status_code == 200


def test_a_deactivated_admin_does_not_count_toward_the_last_admin_guard(client, app_db):
    """Otherwise a switched-off admin keeps the real one from being managed."""
    auth = admin_auth(client)
    me = client.get("/auth/me", headers=auth).json()
    second = client.post(
        "/users",
        headers=auth,
        json={"email": "second@example.com", "full_name": "Second", "role": "admin"},
    ).json()["user"]
    client.patch(f"/users/{second['id']}", headers=auth, json={"is_active": False})

    response = client.patch(f"/users/{me['id']}", headers=auth, json={"role": "analyst"})

    assert response.status_code == 409


def test_resetting_a_password_issues_a_new_temporary_one(client, app_db):
    auth = admin_auth(client)
    created = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()

    response = client.post(f"/users/{created['user']['id']}/reset-password", headers=auth)

    assert response.status_code == 200
    new_password = response.json()["temporary_password"]
    assert new_password != created["temporary_password"]
    assert login(client, email="kemi@example.com", password=new_password).status_code == 200


def test_resetting_a_password_kills_the_target_s_sessions(client, app_db):
    """A reset done because an account is suspected compromised must eject
    whoever is in it, not run alongside them."""
    auth = admin_auth(client)
    created = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()
    stolen = login(
        client, email="kemi@example.com", password=created["temporary_password"]
    ).json()["token"]

    client.post(f"/users/{created['user']['id']}/reset-password", headers=auth)

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {stolen}"}).status_code == 401


def test_an_analyst_cannot_create_deactivate_or_reset(client, app_db):
    admin = admin_auth(client)
    created = client.post(
        "/users",
        headers=admin,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()["user"]
    auth = analyst_auth(client)

    assert client.post("/users", headers=auth, json={"email": "x@example.com", "full_name": "X", "role": "analyst"}).status_code == 403
    assert client.patch(f"/users/{created['id']}", headers=auth, json={"is_active": False}).status_code == 403
    assert client.post(f"/users/{created['id']}/reset-password", headers=auth).status_code == 403


def test_an_unknown_user_id_is_a_404(client, app_db):
    auth = admin_auth(client)

    assert client.patch("/users/nope", headers=auth, json={"is_active": False}).status_code == 404


def test_every_management_action_writes_an_audit_entry(client, app_db):
    auth = admin_auth(client)
    created = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()["user"]
    client.patch(f"/users/{created['id']}", headers=auth, json={"role": "admin"})
    client.patch(f"/users/{created['id']}", headers=auth, json={"is_active": False})
    client.post(f"/users/{created['id']}/reset-password", headers=auth)

    db = get_sessionmaker()()
    try:
        actions = {e.action for e in db.query(AuditLog).all()}
    finally:
        db.close()

    assert AuditAction.USER_CREATED in actions
    assert AuditAction.USER_ROLE_CHANGED in actions
    assert AuditAction.USER_DEACTIVATED in actions
    assert AuditAction.USER_PASSWORD_RESET in actions


def test_no_audit_entry_carries_the_temporary_password(client, app_db):
    auth = admin_auth(client)
    body = client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    ).json()

    db = get_sessionmaker()()
    try:
        blob = " ".join(str(e.detail) for e in db.query(AuditLog).all())
    finally:
        db.close()

    assert body["temporary_password"] not in blob


def test_an_admin_reads_the_audit_log(client, app_db):
    auth = admin_auth(client)
    client.post(
        "/users",
        headers=auth,
        json={"email": "kemi@example.com", "full_name": "Kemi", "role": "analyst"},
    )

    response = client.get("/audit-log", headers=auth)

    assert response.status_code == 200
    assert any(e["action"] == "user_created" for e in response.json())


def test_an_analyst_cannot_read_the_audit_log(client, app_db):
    auth = analyst_auth(client)

    assert client.get("/audit-log", headers=auth).status_code == 403
