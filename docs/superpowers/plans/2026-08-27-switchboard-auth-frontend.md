# Switchboard Auth — Admin Endpoints and Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin signs in through the browser, creates analyst accounts, and manages them — and every analyst signs in to a dashboard that shows only their own work.

**Architecture:** The browser stops talking to the engine. It calls Next.js, which holds an httpOnly session cookie and forwards to the engine with a bearer token. One catch-all route handler does the forwarding, and the existing `request()` chokepoint is repointed at it. The engine gains `/users` endpoints and an audit log so account management stops requiring shell access.

**Tech Stack:** Engine — FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, Python 3.13, `uv`. Frontend — Next.js 16.3.2 App Router, React 19, Tailwind v4, vitest, Testing Library.

**Spec:** `docs/superpowers/specs/2026-08-26-switchboard-auth-design.md`

## Global Constraints

- **DO NOT PUSH.** Commit locally only. Never run `git push` or anything touching a remote.
- Never write to the real Neon database named in `services/analyzer/.env`. Use the test suite's SQLite fixtures. Never read, echo, or quote `.env`.
- NEVER use `git commit --no-verify`. Pre-commit hooks run the gate suite and a secrets scan in both repos; if one fails, fix the cause.
- Work synchronously. Do not start background monitors or wait loops.
- **Engine baselines:** gate lane **679 passed**, whole suite (`-m "not slow and not live"`) **1139 passed**, `tests/test_openapi_contract.py` 6 passed. Current migration head: `0013_owner_scoped_names`.
- **Frontend baseline:** `npx vitest run` **559 passed** across 40 files. Typecheck and lint must stay clean.
- Engine: all new modules begin with `from __future__ import annotations`; migrations use `op.batch_alter_table` and `sa.true()`/`sa.false()`; enum columns use `enum_column()`.
- Engine: regenerate the contract after any schema or route change — `uv run python ../../scripts/export_openapi.py` from `services/analyzer` — and commit `contracts/openapi.json`.
- Both repos: substantial docstrings and comments explaining WHY a choice was made, not what the code does. No em dashes in prose.
- Test emails are lowercase `@example.com`. `users.email` has a CHECK constraint requiring lowercase, and stock `EmailStr` rejects `.test` domains.
- Engine repo: `/home/Temz/fraud-analyzer-engine`, code in `services/analyzer/`. Frontend repo: `/home/Temz/fraud-analyzer-dashboard`.

## What already exists

Engine, from the previous plan:
- `POST /auth/login` → `{token, user}`; `POST /auth/logout`; `GET /auth/me`; `POST /auth/change-password`.
- `app/security/deps.py`: `require_user` (also enforces the password-change gate), `require_admin`, `PUBLIC_PATHS`.
- `app/models/user.py`: `User`, `UserSession`. `User.is_admin`.
- `app/services/session_service.py`, `auth_service.py`, `app/security/passwords.py`, `app/cli.py`.
- `tests/test_route_coverage.py` fails the build if any route lacks an auth dependency.

Frontend, unchanged so far:
- All 9 pages are `"use client"`. No middleware. No server-side engine calls.
- **One chokepoint:** every API call goes through `request()` in `src/services/api-client/client.ts`, whose URL is built by `buildUrl` from `resolveBaseUrl(process.env.NEXT_PUBLIC_API_BASE_URL)`.

---

### Task 1: Audit log

**Files:**
- Create: `services/analyzer/app/models/audit_log.py`
- Create: `services/analyzer/alembic/versions/0014_audit_log.py`
- Create: `services/analyzer/app/services/audit_service.py`
- Modify: `services/analyzer/app/models/__init__.py`, `app/models/enums.py`
- Test: `services/analyzer/tests/test_audit_log.py`

**Interfaces:**
- Produces: `AuditAction` enum; `AuditLog` model (table `audit_logs`); `audit_service.record(db, actor, action, target_type, target_id, detail=None) -> AuditLog`.

The spec keeps this separate from `query_execution_logs` on purpose: one records who changed the system, the other what ran against a customer database. Conflating them makes both harder to read.

- [ ] **Step 1: Add the action enum**

In `app/models/enums.py`, after `UserRole`:

```python
class AuditAction(StrEnum):
    """Administrative acts worth reconstructing months later.

    Deliberately a closed set rather than free text. An audit trail whose
    action names are typed by whoever wrote the call site cannot be filtered
    or counted, and the first person to need it is looking for one specific
    thing under time pressure.
    """

    USER_CREATED = "user_created"
    USER_DEACTIVATED = "user_deactivated"
    USER_REACTIVATED = "user_reactivated"
    USER_ROLE_CHANGED = "user_role_changed"
    USER_PASSWORD_RESET = "user_password_reset"
```

- [ ] **Step 2: Write the failing tests**

Create `services/analyzer/tests/test_audit_log.py`:

```python
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
    assert "password" not in stored
    assert "hunter2-hunter2" not in str(stored)


def test_entries_survive_in_order(session):
    actor = _user(session)
    for _ in range(3):
        audit_service.record(session, actor, AuditAction.USER_CREATED, "user", actor.id)

    assert session.query(AuditLog).count() == 3
```

- [ ] **Step 3: Run and watch them fail**

From `services/analyzer`: `uv run pytest tests/test_audit_log.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.audit_log'`

- [ ] **Step 4: Write the model**

Create `services/analyzer/app/models/audit_log.py`:

```python
"""An append-only record of who changed the system.

Distinct from ``query_execution_logs``, which records queries *running*.
Conflating "who granted this person access" with "what ran against the
customer's database" would make both harder to read, and they are consulted by
different people answering different questions.

Nothing here is ever updated or deleted by application code. An audit trail
that its own service can rewrite is not an audit trail.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, UTCDateTime, new_id, utcnow
from app.models.enums import AuditAction, enum_column


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    #: Who did it. RESTRICT rather than SET NULL: accounts are never deleted,
    #: and an audit entry that has forgotten its actor answers nothing.
    actor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    action: Mapped[AuditAction] = mapped_column(enum_column(AuditAction, length=40), nullable=False)

    #: What kind of thing was acted on, and which one. Free-form rather than a
    #: foreign key because the target may one day be a connection or a chart,
    #: and an audit row must survive its target being removed.
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )
```

Export `AuditLog` and `AuditAction` from `app/models/__init__.py` alongside the others, or `Base.metadata` never learns about the table.

- [ ] **Step 5: Write the service**

Create `services/analyzer/app/services/audit_service.py`:

```python
"""Writing audit entries.

One function, so every call site records the same shape. The alternative -
each router assembling its own entry - produces a table whose rows cannot be
compared to each other six months later.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.enums import AuditAction
from app.models.user import User

#: Keys stripped from ``detail`` before it is stored, whatever the caller says.
#:
#: An audit table is exactly where a credential must never come to rest, and a
#: password-reset entry is the obvious place somebody later adds "the temporary
#: password we issued" for convenience. Refusing at the one chokepoint is the
#: only version of this rule that holds.
_FORBIDDEN_DETAIL_KEYS = frozenset({"password", "new_password", "temporary_password", "token"})


def record(
    db: Session,
    actor: User,
    action: AuditAction,
    target_type: str,
    target_id: str,
    detail: dict | None = None,
) -> AuditLog:
    """Append one entry. Commits, because an audit write must not be rolled
    back by a later failure in the operation it describes."""
    safe = (
        {k: v for k, v in detail.items() if k not in _FORBIDDEN_DETAIL_KEYS}
        if detail
        else None
    )

    entry = AuditLog(
        actor_id=actor.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=safe or None,
    )
    db.add(entry)
    db.commit()
    return entry
```

- [ ] **Step 6: Write the migration**

Create `services/analyzer/alembic/versions/0014_audit_log.py` with `down_revision = "0013_owner_scoped_names"`. Confirm that is the head first with `uv run alembic heads`. Create table `audit_logs` with the columns above, an index on `actor_id` and one on `target_id`, and the foreign key `ON DELETE RESTRICT`. `downgrade()` drops the indexes then the table.

- [ ] **Step 7: Run the tests and the suites**

```
uv run pytest tests/test_audit_log.py -v
uv run pytest tests/test_migrations.py -v
uv run pytest -m "not integration and not slow" -q
```
Expected: new tests pass; migrations pass; gate lane still 679.

- [ ] **Step 8: Commit**

```bash
git add services/analyzer/app services/analyzer/alembic services/analyzer/tests
git commit -m "feat(auth): an append-only record of who changed the system"
```

---

### Task 2: Admin user-management endpoints

**Files:**
- Create: `services/analyzer/app/routers/users.py`
- Create: `services/analyzer/app/services/user_service.py`
- Create: `services/analyzer/app/schemas/user.py`
- Modify: `services/analyzer/app/main.py`, `app/errors.py`
- Test: `services/analyzer/tests/test_users_api.py`

**Interfaces:**
- Consumes: `require_admin`, `require_user`, `audit_service.record`, `passwords.*`, `session_service.revoke_all_for_user`.
- Produces:
  - `GET /users` → `list[UserRead]` (admin only)
  - `POST /users` → `{user: UserRead, temporary_password: str}` (admin only)
  - `PATCH /users/{id}` → `UserRead` — deactivate/reactivate and role change (admin only)
  - `POST /users/{id}/reset-password` → `{temporary_password: str}` (admin only)
  - `GET /audit-log` → `list[AuditEntryRead]` (admin only)
  - `user_service.last_active_admin_guard(db, user, *, changing_role, changing_active)` raising `AppError(FORBIDDEN)`

- [ ] **Step 1: Add error codes**

In `app/errors.py`, add to `ErrorCode` and `HTTP_STATUS_BY_CODE`:

```python
    USER_NOT_FOUND = "USER_NOT_FOUND"        # 404
    DUPLICATE_EMAIL = "DUPLICATE_EMAIL"      # 409
    LAST_ADMIN = "LAST_ADMIN"                # 409
```

`LAST_ADMIN` is 409 rather than 403: the caller is permitted to do this in general, and the request conflicts with the system's current state. A 403 would read as "you may not manage users", which is false and would send an admin looking for a permissions problem that does not exist.

- [ ] **Step 2: Write the failing tests**

Create `services/analyzer/tests/test_users_api.py`. Import `make_user` and `login` from `tests.test_auth_api`. Add `"test_users_api"` to `_INTEGRATION_MODULES` in `tests/conftest.py` — it stands up a TestClient and a database, matching every other API test file.

```python
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
```

- [ ] **Step 3: Run and watch them fail**

`uv run pytest tests/test_users_api.py -v` — expect 404s on `/users`.

- [ ] **Step 4: Write the schemas**

Create `services/analyzer/app/schemas/user.py`. `UserCreate` has `email: EmailStr`, `full_name: str` (1..200), `role: UserRole`. **It has no password field** — the system generates it, so there is nowhere for an admin to supply one. `UserUpdate` has `is_active: bool | None` and `role: UserRole | None`, both optional. `UserCreateResponse` has `user: UserRead` and `temporary_password: str`. `TemporaryPasswordResponse` has `temporary_password: str`. `AuditEntryRead` mirrors `AuditLog` plus `actor_email`. Reuse `UserRead` from `app/schemas/auth.py` rather than defining a second one.

- [ ] **Step 5: Write the service**

Create `services/analyzer/app/services/user_service.py` with:

```python
def count_active_admins(db: Session) -> int:
    """How many admins could still administer this installation."""
    return db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    ) or 0


def guard_last_admin(db: Session, target: User, *, becoming_inactive: bool, becoming_analyst: bool) -> None:
    """Refuse a change that would leave nobody able to administer.

    Enforced here rather than in the router because the router is not the only
    thing that will ever call this, and a rule that lives in one call site is a
    rule until somebody adds a second.
    """
    if not (becoming_inactive or becoming_analyst):
        return
    if target.role is not UserRole.ADMIN or not target.is_active:
        return
    if count_active_admins(db) > 1:
        return
    raise AppError(
        ErrorCode.LAST_ADMIN,
        "This is the only active administrator. Promote somebody else first.",
    )
```

Plus `create_user`, `update_user`, and `reset_password`, each writing its audit entry and each returning the plain temporary password only as a return value, never storing it.

- [ ] **Step 6: Write the router**

Create `services/analyzer/app/routers/users.py` with `dependencies=[Depends(require_admin)]` at the router level, since every endpoint here is admin-only. Register it in `app/main.py`. Add a second small router for `GET /audit-log`, also admin-only.

- [ ] **Step 7: Run everything**

```
uv run pytest tests/test_users_api.py -v
uv run pytest tests/test_route_coverage.py -v
uv run pytest -m "not integration and not slow" -q
uv run pytest -m "not slow and not live" -q
uv run python ../../scripts/export_openapi.py && uv run pytest tests/test_openapi_contract.py -q
```

The route-coverage sweep must still pass — new routers inherit `require_admin`, which composes over `require_user`.

- [ ] **Step 8: Commit**

```bash
git add services/analyzer contracts/
git commit -m "feat(auth): admin account management, with an audit trail"
```

---

### Task 3: The BFF proxy and session cookie

**Files:**
- Create: `src/app/api/[...path]/route.ts`
- Create: `src/app/api/auth/login/route.ts`
- Create: `src/app/api/auth/logout/route.ts`
- Create: `src/lib/session.ts`
- Modify: `src/services/api-client/client.ts`
- Test: `src/lib/session.test.ts`, `src/app/api/proxy.test.ts`

**Interfaces:**
- Produces: `SESSION_COOKIE`, `readSession(req)`, `sessionCookie(token)`, `clearedCookie()`; a catch-all proxy at `/api/*`; `resolveBaseUrl` defaulting to `/api`.

This is the task that makes the whole architecture work. The browser stops holding a token; Next.js holds it in an httpOnly cookie that JavaScript cannot read, so an XSS bug cannot steal the session.

- [ ] **Step 1: Write the session helper and its tests**

Create `src/lib/session.ts`:

```ts
import type { NextRequest } from "next/server";

/**
 * The session cookie, and the rules that make it worth having.
 *
 * `httpOnly` is the whole point: script on the page cannot read it, so an XSS
 * bug that would otherwise be a full account takeover cannot lift the session.
 * `sameSite: "lax"` stops a cross-site form post from riding it, and the proxy
 * additionally requires a custom header on mutating requests, which a
 * cross-site form cannot set and a cross-origin fetch cannot send without
 * passing preflight.
 *
 * `secure` is on except in development, where there is no TLS on localhost and
 * a secure cookie would simply never be stored.
 */
export const SESSION_COOKIE = "switchboard_session";

const MAX_AGE_SECONDS = 12 * 60 * 60;

export function sessionCookie(token: string) {
  return {
    name: SESSION_COOKIE,
    value: token,
    httpOnly: true,
    sameSite: "lax" as const,
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE_SECONDS,
  };
}

/** Same attributes, empty and expired. A cookie cleared with different
 *  attributes than it was set with is not cleared at all. */
export function clearedCookie() {
  return { ...sessionCookie(""), maxAge: 0 };
}

export function readSession(request: NextRequest): string | null {
  return request.cookies.get(SESSION_COOKIE)?.value ?? null;
}
```

Tests in `src/lib/session.test.ts` asserting: the cookie is httpOnly; `sameSite` is lax; `maxAge` matches the engine's 12-hour absolute session so the browser does not hold a cookie the engine has already forgotten; `clearedCookie` keeps the same name and path as `sessionCookie` (a mismatch leaves the original in place); `readSession` returns null when absent.

- [ ] **Step 2: Write the login and logout handlers**

`src/app/api/auth/login/route.ts` posts the credentials to the engine, and on success sets the httpOnly cookie and returns **only the user object** — never the token, or the browser would hold the very thing the cookie exists to hide. On failure it forwards the engine's status and body unchanged, so the login form can distinguish a lockout from a bad password.

`src/app/api/auth/logout/route.ts` calls the engine's logout with the current token, then clears the cookie regardless of the engine's answer — a user who clicks sign-out must end up signed out locally even if the engine is unreachable.

- [ ] **Step 3: Write the catch-all proxy**

Create `src/app/api/[...path]/route.ts`:

```ts
import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, clearedCookie, readSession } from "@/lib/session";

/**
 * Everything the browser asks of the engine passes through here.
 *
 * The browser never holds the session token. It holds an httpOnly cookie this
 * handler reads server-side and exchanges for a bearer header, so script on
 * the page cannot lift the session even if an XSS bug lets it run.
 *
 * ENGINE_BASE_URL is deliberately NOT prefixed NEXT_PUBLIC_. A public variable
 * is inlined into the client bundle, which would publish the engine's address
 * and undo the private-network property this whole design rests on.
 */

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * A header a cross-site form post cannot set, and a cross-origin fetch cannot
 * send without passing preflight. Paired with SameSite=Lax on the cookie it
 * closes CSRF without a token round trip.
 */
const CSRF_HEADER = "x-switchboard-request";

function engineBase(): string {
  const base = process.env.ENGINE_BASE_URL?.trim();
  if (!base) {
    // Failing loudly beats proxying to undefined and reporting a network error
    // the operator cannot act on.
    throw new Error("ENGINE_BASE_URL is not set. Add it to .env.local.");
  }
  return base.replace(/\/+$/, "");
}

async function proxy(request: NextRequest, path: string[]): Promise<NextResponse> {
  const token = readSession(request);
  if (!token) {
    // Answered here rather than at the engine: an unauthenticated caller
    // should cost nothing downstream.
    return NextResponse.json(
      { error_code: "NOT_AUTHENTICATED", message: "Sign in to continue.", detail: null },
      { status: 401 },
    );
  }

  if (MUTATING.has(request.method) && request.headers.get(CSRF_HEADER) !== "1") {
    return NextResponse.json(
      { error_code: "FORBIDDEN", message: "Missing request header.", detail: null },
      { status: 403 },
    );
  }

  const url = new URL(`${engineBase()}/${path.join("/")}`);
  url.search = request.nextUrl.search;

  const headers = new Headers();
  headers.set("authorization", `Bearer ${token}`);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);

  const upstream = await fetch(url, {
    method: request.method,
    headers,
    body: MUTATING.has(request.method) ? await request.text() : undefined,
    // Never let a proxied answer be cached: one analyst's rows must not be
    // served to the next.
    cache: "no-store",
  });

  const body = await upstream.text();
  const response = new NextResponse(body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });

  // The engine rejected the session, so the cookie is worthless. Leaving it
  // set makes every later request retry a dead session forever.
  if (upstream.status === 401) response.cookies.set(clearedCookie());

  return response;
}

type Context = { params: Promise<{ path: string[] }> };

async function handler(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return proxy(request, path);
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
```

Note `SESSION_COOKIE` is imported for the cookie name used by `clearedCookie`; if your editor reports it unused, drop it from the import rather than leaving a lint error.

- [ ] **Step 4: Repoint the chokepoint**

In `src/services/api-client/client.ts`, change `resolveBaseUrl` to default to `/api` instead of throwing when `NEXT_PUBLIC_API_BASE_URL` is unset, and have `buildUrl` construct a relative URL against `window.location.origin`. Add the `X-Switchboard-Request: 1` header to every mutating request in `request()`.

Update `src/services/api-client/client.test.ts`, which currently asserts `resolveBaseUrl("")` throws.

- [ ] **Step 5: Test the proxy**

`src/app/api/proxy.test.ts` asserting: no cookie yields 401 without the engine being called; a mutating request without the custom header yields 403; a GET without it succeeds; the bearer token is attached; the engine's status and body pass through unchanged; a 401 from the engine clears the cookie; **the token never appears in any response body or header sent to the browser**.

- [ ] **Step 6: Run and commit**

```
npx tsc --noEmit && npx eslint src --ext .ts,.tsx && npx vitest run
git add -A && git commit -m "feat: the browser talks to Next.js, which holds the session"
```

---

### Task 4: Login page, route guards, and the forced password change

**Files:**
- Create: `src/app/login/page.tsx`, `src/app/change-password/page.tsx`
- Create: `src/middleware.ts`
- Create: `src/services/auth/useSession.ts`
- Modify: `src/app/(app)/layout.tsx`, `src/components/AppShell.tsx`
- Test: `src/app/login/page.test.tsx`, `src/services/auth/useSession.test.ts`, `src/middleware.test.ts`

**Interfaces:**
- Consumes: `SESSION_COOKIE` from Task 3's `src/lib/session.ts`.
- Produces:
  - `useSession(): Session` where `Session = {user: UserRead | null; loading: boolean; error: string | null; mustChangePassword: boolean; signOut: () => Promise<void>}`
  - `middleware` redirecting unauthenticated requests to `/login?next=...`

**This task adds `UserRead` to `src/contracts/api.ts`**, because `useSession` types against it. Mirror the shape the engine returns from `GET /auth/me`:

```ts
export interface UserRead {
	id: string;
	email: string;
	full_name: string;
	role: "admin" | "analyst";
	is_active: boolean;
	must_change_password: boolean;
	last_login_at: string | null;
	created_at: string;
}
```

Task 5 adds the remaining user types (`UserCreate`, `UserUpdate`, `UserCreateResponse`, `AuditEntryRead`) on top of this one. Do not define a second `UserRead` there.

- [ ] **Step 1: Middleware**

Create `src/middleware.ts`:

```ts
import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE } from "@/lib/session";

/**
 * Sends a signed-out visitor to the login page instead of a broken dashboard.
 *
 * This is CONVENIENCE, NOT A CONTROL. It checks only that a cookie is present,
 * never that it is valid: validity is the engine's to decide, and the engine
 * re-checks it on every single request. Anyone reading this should not come
 * away believing the cookie's presence means somebody is authenticated - a
 * forged cookie sails past here and is refused one hop later.
 *
 * The value is purely that a visitor with a dead session sees a login form
 * rather than a page that renders and then fails every fetch inside it.
 */

const PUBLIC_PREFIXES = ["/login", "/api/auth/login", "/_next", "/favicon"];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    return NextResponse.next();
  }

  if (request.cookies.get(SESSION_COOKIE)) {
    return NextResponse.next();
  }

  const login = request.nextUrl.clone();
  login.pathname = "/login";
  // Carry the deep link so signing in lands where the visitor was going,
  // rather than dumping everyone on the home page.
  login.search = `?next=${encodeURIComponent(pathname + request.nextUrl.search)}`;
  return NextResponse.redirect(login);
}

export const config = {
  // Everything except Next's own static output. The API routes are included
  // deliberately: an expired session should get one redirect, not a page full
  // of 401s.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
```

- [ ] **Step 2: `useSession`**

Create `src/services/auth/useSession.ts`:

```ts
"use client";

import { useCallback, useEffect, useState } from "react";
import type { UserRead } from "@/contracts/api";

/**
 * Who is signed in, for the shell and the guards.
 *
 * PASSWORD_CHANGE_REQUIRED is surfaced as its own state rather than as an
 * error. The engine refuses every route but the password change while that
 * flag is set, so treating it as a failure would show an error screen to
 * somebody whose account is working exactly as designed and who needs to be
 * sent one click away.
 */
export interface Session {
  user: UserRead | null;
  loading: boolean;
  error: string | null;
  mustChangePassword: boolean;
  signOut: () => Promise<void>;
}

export function useSession(): Session {
  const [user, setUser] = useState<UserRead | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mustChangePassword, setMustChange] = useState(false);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const response = await fetch("/api/auth/me", { cache: "no-store" });
        const body = await response.json().catch(() => null);
        if (cancelled) return;

        if (response.ok) {
          setUser(body);
          setMustChange(body?.must_change_password === true);
        } else if (body?.error_code === "PASSWORD_CHANGE_REQUIRED") {
          setMustChange(true);
        } else {
          setError(body?.message ?? "Could not load your account.");
        }
      } catch {
        if (!cancelled) setError("Could not reach the server.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const signOut = useCallback(async () => {
    // The header is required by the proxy on every mutating request.
    await fetch("/api/auth/logout", {
      method: "POST",
      headers: { "X-Switchboard-Request": "1" },
    }).catch(() => undefined);
    // A full navigation rather than a router push: it discards every cached
    // fetch and component state belonging to the person signing out.
    window.location.href = "/login";
  }, []);

  return { user, loading, error, mustChangePassword, signOut };
}
```

- [ ] **Step 3: The login page**

Email, password, submit. Renders the engine's message for a failed login unchanged — the engine deliberately gives the same wording for an unknown email and a wrong password, and the frontend must not helpfully distinguish them. A lockout shows the engine's own "try again in N minutes". After success, redirect to `?next=` if present, otherwise `/`.

- [ ] **Step 4: The forced change page**

Shown when `must_change_password` is set. Current password, new password, confirm. On success, redirect into the app. It must be reachable while the rest of the app is refused, which is why the engine gates it on `current_user` rather than `require_user`.

- [ ] **Step 5: Wire the shell**

`(app)/layout.tsx` uses `useSession`, redirects to `/change-password` when required, and passes the user to `AppShell`. `AppShell` gains a footer block showing the signed-in email and role, and a sign-out control.

**Admin-only navigation is hidden from analysts, and the comment must say plainly that this is cosmetic** — the engine refuses the request regardless, and anybody reading this code should know hiding the link is not the control.

- [ ] **Step 6: Tests**

The login page submits and redirects; a failed login shows the engine's message verbatim; middleware redirects without a cookie and passes with one; middleware preserves `?next=`; `useSession` surfaces `must_change_password`; the shell hides admin nav from an analyst and shows it to an admin.

- [ ] **Step 7: Run and commit**

```
npx tsc --noEmit && npx eslint src --ext .ts,.tsx && npx vitest run
git add -A && git commit -m "feat: sign in, stay signed in, and change a temporary password"
```

---

### Task 5: Admin screens

**Files:**
- Create: `src/app/(app)/admin/users/page.tsx`, `src/app/(app)/admin/audit/page.tsx`
- Create: `src/components/admin/UserTable.tsx`, `src/components/admin/CreateUserDialog.tsx`, `src/components/admin/TemporaryPasswordPanel.tsx`
- Modify: `src/contracts/api.ts`, `src/services/api-client/` (add the user calls)
- Test: `src/components/admin/UserTable.test.tsx`, `src/components/admin/CreateUserDialog.test.tsx`, `src/app/(app)/admin/users/page.test.tsx`

- [ ] **Step 1: Contract types**

Add `UserCreate`, `UserUpdate`, `UserCreateResponse` and `AuditEntryRead` to `src/contracts/api.ts`, mirroring the regenerated `contracts/openapi.json`. `UserRead` already exists — Task 4 added it because `useSession` types against it. Do not define a second one.

- [ ] **Step 2: The user table**

Columns: name, email, role, state, last sign-in. Row actions: deactivate/reactivate, reset password, change role. Deactivated accounts stay listed and visibly inactive rather than disappearing — the spec's whole offboarding model is that work and attribution survive.

- [ ] **Step 3: The create dialog**

Name, email, role. **No password field** — the system generates it. On success, show `TemporaryPasswordPanel`.

- [ ] **Step 4: The temporary-password panel**

Shows the generated password once, with a copy control and an unambiguous warning that it will not be shown again, plus the 72-hour expiry. It must not persist anywhere: not in localStorage, not in a toast that survives navigation, not in the URL.

- [ ] **Step 5: Guard the last admin in the UI too**

Disable the deactivate and demote controls for the only active admin, with a tooltip explaining why. The engine refuses it regardless; this is so an admin learns the rule before hitting an error, not instead of the engine enforcing it.

- [ ] **Step 6: Tests**

The table renders roles and states; a deactivated user is shown as inactive rather than hidden; the create dialog has no password field; the temporary password renders once and is not written to storage; the last active admin's destructive controls are disabled; an analyst navigating to `/admin/users` sees a refusal rather than a broken page.

- [ ] **Step 7: Run and commit**

```
npx tsc --noEmit && npx eslint src --ext .ts,.tsx && npx vitest run
git add -A && git commit -m "feat: admin screens for account management"
```

---

### Task 6: End-to-end verification and the seed

**Files:**
- Modify: `scripts/dev-seed.mjs`, `scripts/smoke.mjs`
- Modify: `README.md` in both repos

- [ ] **Step 1: Both scripts must sign in**

`dev-seed.mjs` and `smoke.mjs` currently call the engine unauthenticated and will fail against it entirely. Give each a login step. The smoke lane needs an admin account to exist; document how to create one, or have the script create it via the CLI when missing.

- [ ] **Step 2: Run the whole thing by hand**

With the engine and Postgres running, and migrations applied:

```
cd services/analyzer && uv run fae create-admin
cd ../../.. && cd fraud-analyzer-dashboard && npm run dev
```

Sign in as the admin. Create an analyst. Sign out. Sign in as the analyst with the temporary password. Confirm the forced change screen appears and nothing else is reachable. Change the password. Confirm the dashboard loads, the analyst sees no admin navigation, and `/admin/users` is refused.

Record what you actually observed, including anything that looked wrong.

- [ ] **Step 3: Document it**

A "Signing in" section in the dashboard README covering the environment variables (`ENGINE_BASE_URL` server-only; `NEXT_PUBLIC_API_BASE_URL` no longer used), how to create the first admin, and what an analyst's first sign-in looks like.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "chore: seed, smoke and docs for an authenticated engine"
```

## What this plan does not cover

Publishing and the query freeze, and the rename to Switchboard with its new icon. Both are in the spec; each gets its own plan.
