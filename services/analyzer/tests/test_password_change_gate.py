"""An account holding a temporary password can do exactly one thing.

Task 5 built the gate itself: ``require_user`` in ``app/security/deps.py``
raises ``PASSWORD_CHANGE_REQUIRED`` whenever ``user.must_change_password`` is
set, and every router in ``app/routers/`` is wired to ``require_user`` (or
``require_admin``, which is built on it) at the router level. This module
writes no new behaviour. It proves the one rule that is enforced across every
endpoint at once, which is exactly why it earns its own test module rather
than a few extra cases tacked onto ``test_auth_api.py``: a reviewer should be
able to reject this behaviour independently of anything else Task 5 touched.

Email domain note: the brief's own illustrative code uses ``@b.test``, but
``LoginRequest.email`` is a pydantic ``EmailStr``, and email-validator
hard-rejects the ``.test`` special-use TLD regardless of deliverability
checks. ``login()`` below goes through the real HTTP request body, not a
direct ORM insert, so it has to survive that validation - hence
``@example.com``, the same domain the rest of the suite's fixtures already
use (see ``tests/conftest.py``'s ``admin_client``). ``users.email`` also
carries a ``CHECK (email = lower(email))`` constraint, so every address here
is lowercase from the start rather than relying on the app to fold it.
"""

from __future__ import annotations

from datetime import timedelta

from app.db.app_state import get_sessionmaker
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User
from tests.test_auth_api import login, make_user

TEMPORARY = "a-temporary-password"


def _temp_user(email="new@example.com", role=UserRole.ANALYST):
    """Insert a user carrying an admin-issued, not-yet-expired temporary
    password.

    ``make_user`` forwards ``must_change_password=True`` straight to the
    ``User`` constructor (it passes unknown kwargs through), but it has no
    keyword for the expiry timestamp because that column is meant to be set
    once, deliberately, by whatever issued the password - here, that means
    reaching back into the row after the insert rather than teaching
    ``make_user`` a field only this module needs.
    """
    user = make_user(
        email=email,
        role=role,
        password=TEMPORARY,
        must_change_password=True,
    )
    db = get_sessionmaker()()
    try:
        stored = db.get(User, user.id)
        stored.temp_password_expires_at = utcnow() + timedelta(hours=72)
        db.commit()
    finally:
        db.close()
    return user


def test_a_temporary_password_still_logs_in(client, app_db):
    """It has to. Refusing the login would leave no way to reach the change
    screen at all."""
    _temp_user()

    assert login(client, email="new@example.com", password=TEMPORARY).status_code == 200


def test_every_other_endpoint_is_refused(client, app_db):
    _temp_user()
    token = login(client, email="new@example.com", password=TEMPORARY).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    response = client.get("/connections", headers=auth)

    assert response.status_code == 403
    assert response.json()["error_code"] == "PASSWORD_CHANGE_REQUIRED"


def test_the_refusal_applies_to_administrators_too(client, app_db):
    """A reset administrator is not exempt: the point is that the credential
    was issued by somebody else, and role does not change that."""
    _temp_user(email="boss@example.com", role=UserRole.ADMIN)
    token = login(client, email="boss@example.com", password=TEMPORARY).json()["token"]

    response = client.get("/connections", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["error_code"] == "PASSWORD_CHANGE_REQUIRED"


def test_the_change_endpoint_itself_is_allowed(client, app_db):
    """The one exception, or the account is bricked."""
    _temp_user()
    token = login(client, email="new@example.com", password=TEMPORARY).json()["token"]

    response = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": TEMPORARY, "new_password": "a-chosen-password"},
    )

    assert response.status_code == 204


def test_me_is_allowed_so_the_app_can_route_to_the_change_screen(client, app_db):
    """The frontend has to learn *why* it is being refused before it can show
    the right screen, and /auth/me is where it asks."""
    _temp_user()
    token = login(client, email="new@example.com", password=TEMPORARY).json()["token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True


def test_the_rest_of_the_app_opens_once_the_password_is_changed(client, app_db):
    _temp_user()
    token = login(client, email="new@example.com", password=TEMPORARY).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    client.post(
        "/auth/change-password",
        headers=auth,
        json={"current_password": TEMPORARY, "new_password": "a-chosen-password"},
    )

    assert client.get("/connections", headers=auth).status_code == 200


def test_an_expired_temporary_password_no_longer_logs_in(client, app_db):
    user = _temp_user()
    db = get_sessionmaker()()
    try:
        stored = db.get(User, user.id)
        stored.temp_password_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert login(client, email="new@example.com", password=TEMPORARY).status_code == 401
