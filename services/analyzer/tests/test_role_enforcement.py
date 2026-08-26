"""Analysts are refused the endpoints that manage the system."""

from __future__ import annotations

import pytest

from app.models.enums import UserRole
from tests.test_auth_api import login, make_user

PASSWORD = "a-perfectly-fine-password"


@pytest.fixture
def analyst_auth(client, app_db):
    make_user(email="role-analyst@example.com", role=UserRole.ANALYST)
    token = login(client, email="role-analyst@example.com").json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_auth(client, app_db):
    make_user(email="role-admin@example.com", role=UserRole.ADMIN)
    token = login(client, email="role-admin@example.com").json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_an_analyst_cannot_create_a_connection(client, analyst_auth, target_sqlite):
    response = client.post(
        "/connections",
        headers=analyst_auth,
        json={"name": "mine", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"


def test_an_admin_can_create_a_connection(client, admin_auth, target_sqlite):
    response = client.post(
        "/connections",
        headers=admin_auth,
        json={"name": "theirs", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )

    assert response.status_code == 201, response.text


def test_an_analyst_can_list_connections(client, analyst_auth):
    """Listing is not managing. Analysts query these databases, so they have to
    be able to see which ones exist."""
    assert client.get("/connections", headers=analyst_auth).status_code == 200


def test_an_analyst_cannot_delete_a_connection(client, analyst_auth, admin_auth, target_sqlite):
    created = client.post(
        "/connections",
        headers=admin_auth,
        json={"name": "theirs", "db_type": "sqlite", "sqlite_path": target_sqlite},
    ).json()["connection"]

    response = client.delete(f"/connections/{created['id']}", headers=analyst_auth)

    assert response.status_code == 403


def test_an_analyst_cannot_pause_or_resume_a_connection(client, analyst_auth, admin_auth, target_sqlite):
    """Disconnect and reconnect are management actions symmetric with delete:
    the brief that shipped this suite named disconnect ("pause") explicitly as
    admin-only but did not mention reconnect. Leaving reconnect open to an
    analyst would let anyone put a paused connection back into service, which
    is exactly the kind of asymmetry a coverage sweep cannot catch because
    both endpoints are equally "guarded" - just by the wrong role."""
    created = client.post(
        "/connections",
        headers=admin_auth,
        json={"name": "pausable", "db_type": "sqlite", "sqlite_path": target_sqlite},
    ).json()["connection"]
    connection_id = created["id"]

    disconnect = client.post(
        f"/connections/{connection_id}/disconnect", headers=analyst_auth
    )
    assert disconnect.status_code == 403

    # An admin pauses it for real, then the analyst is checked against the
    # endpoint that would undo that.
    admin_pause = client.post(
        f"/connections/{connection_id}/disconnect", headers=admin_auth
    )
    assert admin_pause.status_code == 200, admin_pause.text

    reconnect = client.post(
        f"/connections/{connection_id}/reconnect", headers=analyst_auth
    )
    assert reconnect.status_code == 403


def test_an_unauthenticated_caller_gets_401_not_403(client, app_db):
    """The two are different answers to different questions: 401 means "sign
    in", 403 means "signed in, still not allowed". A client that conflates them
    either redirects a permitted user to login or shows an access error to
    somebody who simply needs to sign in."""
    assert client.get("/connections").status_code == 401


def test_a_locked_out_password_change_account_is_refused_everywhere_but_the_escape_hatch(
    client, app_db
):
    """A must_change_password account is meant to be able to reach exactly
    two things beyond auth/login and auth/logout: /auth/me (so a restored
    session can discover the flag) and /auth/change-password (the only way to
    clear it). Everything else - including a plain read like listing
    connections - must refuse it with the same PASSWORD_CHANGE_REQUIRED code
    the require_user dependency raises, not a silent pass-through."""
    make_user(email="role-gated@example.com", role=UserRole.ADMIN, must_change_password=True)
    token = login(client, email="role-gated@example.com").json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    me = client.get("/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True

    blocked = client.get("/connections", headers=auth)
    assert blocked.status_code == 403
    assert blocked.json()["error_code"] == "PASSWORD_CHANGE_REQUIRED"

    fixed = client.post(
        "/auth/change-password",
        headers=auth,
        json={"current_password": PASSWORD, "new_password": "a-new-fine-password"},
    )
    assert fixed.status_code == 204

    # The gate is gone once the password is changed, even though it is the
    # same session token throughout - change-password revokes every *other*
    # session but restores this one, per app/routers/auth.py.
    unblocked = client.get("/connections", headers=auth)
    assert unblocked.status_code == 200
