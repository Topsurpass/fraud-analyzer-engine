# Switchboard Auth — Engine Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the engine accounts, sessions, two roles, and per-user ownership, so that no endpoint is reachable without authentication and an analyst sees only their own work.

**Architecture:** Opaque server-side sessions in a `sessions` table, resolved on every request by a FastAPI dependency that also loads the user's role and active flag. Passwords are argon2id. The first admin is created by a CLI command; nothing on the network can mint one. Ownership is a nullable `owner_id` on the resource tables, filtered in the service layer rather than the router.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, argon2-cffi, Typer, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-08-26-switchboard-auth-design.md`

## Scope

This plan covers spec sequencing steps 1–3 plus the route-coverage test. The spec notes these must land together: the app is unusable between them, so they go on one branch `feat/auth-engine` and merge as a unit.

**Out of scope, each getting its own plan once this lands:** admin user-management endpoints and audit log (step 4), publishing and freezing (step 5), the Next.js proxy and login UI (steps 6–7), the rename (step 8).

## Global Constraints

- Python `>=3.12`; the venv runs 3.13. All code uses `from __future__ import annotations`.
- Migrations use `op.batch_alter_table` for any column addition — SQLite is a supported app backend and cannot `ALTER` in place.
- Booleans use `server_default=sa.false()`, never `text("0")`. Postgres rejects the integer form and refuses to boot; SQLite accepts it, so only a real Postgres start catches the mistake.
- Enum columns use `enum_column()` from `app/models/enums.py`, which stores the enum *value* and needs no migration to add members.
- Gate tests must be deterministic, local, free, and under 2s. Run with `uv run pytest -m "not integration and not slow"`.
- Never use `--no-verify`. The pre-commit hook runs the gate suite and a secrets scan.
- No secrets in any committed file. `services/analyzer/.env` holds live credentials and must never be read into a doc, a test fixture, or a commit.
- After any schema or route change, regenerate the contract: `uv run python ../../scripts/export_openapi.py`. CI has a staleness check.
- All work happens in `services/analyzer/` unless a path says otherwise.

---

### Task 1: Password hashing and strength rules

**Files:**
- Create: `app/security/passwords.py`
- Modify: `pyproject.toml` (add `argon2-cffi`)
- Modify: `app/errors.py` (add `WEAK_PASSWORD`)
- Test: `tests/test_passwords.py`

**Interfaces:**
- Consumes: `AppError`, `ErrorCode` from `app/errors.py`.
- Produces:
  - `hash_password(plaintext: str) -> str`
  - `verify_password(plaintext: str, hashed: str) -> bool`
  - `validate_password_strength(plaintext: str) -> None` — raises `AppError(WEAK_PASSWORD)`
  - `generate_temporary_password() -> str`
  - `MIN_PASSWORD_LENGTH: int = 12`

- [ ] **Step 1: Add the dependency**

```bash
cd services/analyzer
uv add argon2-cffi
```

- [ ] **Step 2: Add the error code**

In `app/errors.py`, inside `class ErrorCode`, under the `--- 400` group, after `SQL_TOO_LONG`:

```python
    WEAK_PASSWORD = "WEAK_PASSWORD"
```

And in `HTTP_STATUS_BY_CODE`, add:

```python
    ErrorCode.WEAK_PASSWORD: 400,
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_passwords.py`:

```python
"""Password hashing and the rules for choosing one."""

from __future__ import annotations

import pytest

from app.errors import AppError, ErrorCode
from app.security.passwords import (
    MIN_PASSWORD_LENGTH,
    generate_temporary_password,
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_a_password_verifies_against_its_own_hash():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_a_wrong_password_does_not_verify():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("Correct horse battery staple", hashed) is False


def test_the_hash_does_not_contain_the_password():
    """The obvious property, asserted because it is the whole point."""
    hashed = hash_password("correct horse battery staple")
    assert "correct" not in hashed


def test_the_same_password_hashes_differently_every_time():
    """Argon2 salts per call, so two users with one password are not
    identifiable as such from the database."""
    assert hash_password("correct horse battery staple") != hash_password(
        "correct horse battery staple"
    )


def test_verify_returns_false_for_a_hash_it_cannot_parse():
    """A corrupt or truncated hash column must read as 'wrong password', not
    raise - otherwise a damaged row turns a login into a 500 that leaks the
    shape of the problem."""
    assert verify_password("anything", "not-a-hash") is False


def test_a_short_password_is_refused():
    with pytest.raises(AppError) as caught:
        validate_password_strength("a" * (MIN_PASSWORD_LENGTH - 1))
    assert caught.value.error_code is ErrorCode.WEAK_PASSWORD


def test_a_long_enough_password_is_accepted():
    validate_password_strength("a-perfectly-fine-password")


def test_a_common_password_is_refused_however_long():
    """Length alone is not strength: 'password123456' clears twelve characters
    and is in every wordlist an attacker owns."""
    with pytest.raises(AppError) as caught:
        validate_password_strength("password123456")
    assert caught.value.error_code is ErrorCode.WEAK_PASSWORD


def test_the_refusal_says_what_is_wrong():
    """A rejection with no reason makes a user try the same class of password
    again."""
    with pytest.raises(AppError) as caught:
        validate_password_strength("short")
    assert str(MIN_PASSWORD_LENGTH) in caught.value.message


def test_a_generated_temporary_password_passes_the_rules():
    """It is handed out to a real person, so it has to satisfy the same policy
    they will be held to."""
    validate_password_strength(generate_temporary_password())


def test_generated_temporary_passwords_are_not_repeated():
    assert len({generate_temporary_password() for _ in range(50)}) == 50
```

- [ ] **Step 4: Run the tests and watch them fail**

Run: `uv run pytest tests/test_passwords.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.security.passwords'`

- [ ] **Step 5: Implement**

Create `app/security/passwords.py`:

```python
"""Hashing passwords, and the rules for choosing one.

Argon2id rather than bcrypt: it is the current password-hashing competition
winner, it resists GPU attack by being memory-hard, and ``argon2-cffi`` picks
sane parameters without this module inventing any.

Nothing here reads or writes the database. Keeping it pure is what makes the
policy testable without a fixture, and it keeps the one security-critical
primitive in a file small enough to read in full.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

from app.errors import AppError, ErrorCode

#: Long enough that a stolen hash is not worth a dictionary run. NIST no longer
#: recommends composition rules (a symbol, a digit, a capital) because they
#: push people toward "Password1!" - length and a blocklist do more.
MIN_PASSWORD_LENGTH = 12

#: The passwords an attacker tries first. Deliberately tiny and inlined: a full
#: wordlist is a data-file dependency for a service with a handful of accounts,
#: and these cover the ones a person actually picks under protest.
_COMMON = frozenset(
    {
        "password",
        "password1",
        "password123",
        "password1234",
        "password12345",
        "password123456",
        "passw0rd",
        "qwertyuiop",
        "1234567890",
        "123456789012",
        "letmein",
        "welcome",
        "welcome123",
        "administrator",
        "changeme",
        "changeme123",
        "switchboard",
        "switchboard1",
    }
)

_hasher = PasswordHasher()

#: Unambiguous alphabet: no O/0, l/1/I. A temporary password is read aloud,
#: typed from a screenshot, or copied out of a chat message, and a character
#: nobody can identify turns into a support conversation.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_TEMP_LENGTH = 16


def hash_password(plaintext: str) -> str:
    """Argon2id hash, salt and parameters embedded in the returned string."""
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Whether the password matches. Never raises.

    A malformed hash reads as "wrong password" rather than an exception: a
    corrupt or truncated column would otherwise turn a login attempt into a 500,
    which both breaks the account and tells an attacker something is unusual
    about it.
    """
    try:
        return _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def validate_password_strength(plaintext: str) -> None:
    """Raise ``AppError`` if the password is not allowed. Silent if it is."""
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise AppError(
            ErrorCode.WEAK_PASSWORD,
            f"A password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )
    if plaintext.strip().lower() in _COMMON:
        raise AppError(
            ErrorCode.WEAK_PASSWORD,
            "That password is one of the first an attacker tries. Choose another.",
        )


def generate_temporary_password() -> str:
    """A random password for an account whose owner will replace it.

    ``secrets``, never ``random``: the latter is seeded predictably and is not
    fit for anything that guards an account.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TEMP_LENGTH))
```

- [ ] **Step 6: Run the tests and watch them pass**

Run: `uv run pytest tests/test_passwords.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 7: Commit**

```bash
git add app/security/passwords.py app/errors.py tests/test_passwords.py pyproject.toml uv.lock
git commit -m "feat(auth): argon2id password hashing and strength rules"
```

---

### Task 2: User and session models, and their migration

**Files:**
- Create: `app/models/user.py`
- Create: `alembic/versions/0011_users_and_sessions.py`
- Modify: `app/models/__init__.py`
- Modify: `app/models/enums.py`
- Test: `tests/test_migrations.py` (append)

**Interfaces:**
- Consumes: `Base`, `TimestampMixin`, `new_id` from `app/models/base.py`; `enum_column` from `app/models/enums.py`.
- Produces:
  - `UserRole` enum with `ADMIN = "admin"`, `ANALYST = "analyst"`
  - `User` model, `__tablename__ = "users"`
  - `UserSession` model, `__tablename__ = "sessions"`

The session model is called `UserSession`, not `Session`. `sqlalchemy.orm.Session` is imported in nearly every service module in this codebase, and a second `Session` in scope is a name collision waiting to be resolved wrongly at 2am.

- [ ] **Step 1: Add the role enum**

In `app/models/enums.py`, after `class FlagSeverity`:

```python
class UserRole(StrEnum):
    """What a signed-in person may do.

    Two roles on purpose. A third ("viewer", "supervisor") is a permission
    system in disguise, and the moment roles need combining they should become
    a permission table rather than a longer enum.
    """

    ADMIN = "admin"
    ANALYST = "analyst"
```

- [ ] **Step 2: Write the failing migration test**

Append to `tests/test_migrations.py`:

```python
def test_users_and_sessions_are_created(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "sessions"} <= tables


def test_a_session_is_removed_with_its_user(tmp_path, alembic_for):
    """Sessions cascade even though users are never deleted.

    The spec forbids deleting accounts, so this should never fire in practice.
    It is here because "never happens" and "cannot happen" are different, and a
    session row pointing at a missing user would authenticate nobody while
    looking like it authenticates someone.
    """
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash, role, "
                "is_active, must_change_password, failed_login_count, "
                "created_at, updated_at) VALUES ('u1', 'a@b.test', 'A', 'x', "
                "'admin', 1, 0, 0, '2026-08-26', '2026-08-26')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, "
                "last_seen_at) VALUES ('s1', 'u1', '2026-08-26', '2026-08-27', "
                "'2026-08-26')"
            )
        )
        conn.execute(text("DELETE FROM users WHERE id = 'u1'"))
        remaining = conn.execute(text("SELECT count(*) FROM sessions")).scalar_one()
    assert remaining == 0


def test_users_and_sessions_survive_a_downgrade_and_reapply(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0010_surge_threshold")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert "users" not in tables
    assert "sessions" not in tables

    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "sessions"} <= tables
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_migrations.py -k "users_and_sessions or session_is_removed" -v`
Expected: FAIL — the tables do not exist.

- [ ] **Step 4: Write the model**

Create `app/models/user.py`:

```python
"""Accounts and the sessions they hold open.

The session table exists because a JSON web token cannot be taken away. An
account is deactivated rather than deleted, and a deactivation that takes
effect whenever the holder's token happens to expire is not a deactivation - so
identity is looked up per request against a row this service controls, and
switching off an account ends its sessions at once.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import UserRole, enum_column


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    #: Stored lowercased rather than relying on a case-insensitive collation.
    #: This app runs on both SQLite and Postgres and ``citext`` exists on only
    #: one of them, so case folding happens in Python where both agree.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole), nullable=False, default=UserRole.ANALYST
    )

    #: False blocks login and invalidates live sessions. Accounts are never
    #: deleted, so this is the whole of offboarding.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Set on every admin-issued credential. While true the account may call
    #: nothing but the password change.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    #: When the issued temporary password stops working. Null once the user has
    #: chosen their own, because a password they picked does not expire.
    temp_password_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Null for the first admin, who is created by the CLI with nobody logged in.
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


class UserSession(Base):
    """One signed-in browser.

    Named ``UserSession`` because ``sqlalchemy.orm.Session`` is imported in
    nearly every service module here, and two things called ``Session`` in one
    file is a collision that gets resolved wrongly under pressure.
    """

    __tablename__ = "sessions"

    #: A SHA-256 digest of the token, never the token. A database dump
    #: otherwise hands over every live session, which is the same failure as
    #: storing passwords in the clear, one layer up.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Absolute expiry, fixed at creation and never extended.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Moved forward on use, for the idle timeout.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
```

- [ ] **Step 5: Export the models**

In `app/models/__init__.py`, add `User` and `UserSession` to the imports and to `__all__`, following the pattern already used for the other models. The Alembic autogenerate target and `Base.metadata.create_all` both rely on the module being imported, so a model missing here exists in Python and not in the database.

- [ ] **Step 6: Write the migration**

Create `alembic/versions/0011_users_and_sessions.py`:

```python
"""Accounts and sessions.

Revision ID: 0011_users_and_sessions
Revises: 0010_surge_threshold
Create Date: 2026-08-26

Adds only the two new tables. Ownership columns on the existing tables are a
separate migration, so that this one can be reverted without touching data the
app already depends on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.enums import UserRole, enum_column

revision = "0011_users_and_sessions"
down_revision = "0010_surge_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            enum_column(UserRole),
            nullable=False,
            server_default=UserRole.ANALYST.value,
        ),
        # sa.true()/sa.false(), never text("1"): SQLite accepts an integer
        # default for a boolean and Postgres rejects it outright, so the
        # integer form passes the whole test suite and refuses to boot in
        # production.
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("temp_password_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
```

- [ ] **Step 7: Run the migration tests**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: PASS, including the existing migration tests.

- [ ] **Step 8: Check the schema verifier agrees**

Run: `uv run pytest tests/ -m "not integration and not slow" -q`
Expected: PASS. `verify_schema()` in `app/db/migrate.py` compares model metadata to the migrated database; a mismatch between the model and the migration fails here and nowhere else.

- [ ] **Step 9: Commit**

```bash
git add app/models/user.py app/models/__init__.py app/models/enums.py \
        alembic/versions/0011_users_and_sessions.py tests/test_migrations.py
git commit -m "feat(auth): user and session tables"
```

---

### Task 3: Session service

**Files:**
- Create: `app/services/session_service.py`
- Modify: `app/config.py`
- Test: `tests/test_session_service.py`

**Interfaces:**
- Consumes: `User`, `UserSession` from `app/models/user.py`.
- Produces:
  - `issue(db: Session, user: User, ip: str | None, user_agent: str | None) -> str` — returns the raw token, which is the only time it exists
  - `resolve(db: Session, raw_token: str) -> User | None`
  - `revoke(db: Session, raw_token: str) -> None`
  - `revoke_all_for_user(db: Session, user_id: str) -> int`
  - `purge_expired(db: Session) -> int`

- [ ] **Step 1: Add the settings**

In `app/config.py`, beside the other settings:

```python
    # Sessions.
    #
    # Absolute lifetime is not extended by use: a session that has existed for
    # twelve hours ends whether or not somebody is still typing, which bounds
    # how long a stolen cookie is worth anything.
    session_absolute_hours: int = Field(default=12, gt=0)
    #: Idle timeout. Shorter than the absolute lifetime, and refreshed on use.
    session_idle_hours: int = Field(default=8, gt=0)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_session_service.py`:

```python
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
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_session_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.session_service'`

- [ ] **Step 4: Implement**

Create `app/services/session_service.py`:

```python
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
```

- [ ] **Step 5: Run and watch it pass**

Run: `uv run pytest tests/test_session_service.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/session_service.py app/config.py tests/test_session_service.py
git commit -m "feat(auth): server-side sessions with immediate revocation"
```

---

### Task 4: Login, logout, and the current user

**Files:**
- Create: `app/services/auth_service.py`
- Create: `app/routers/auth.py`
- Create: `app/schemas/auth.py`
- Modify: `app/errors.py`
- Modify: `app/main.py`
- Test: `tests/test_auth_api.py`

**Interfaces:**
- Consumes: `session_service.issue/resolve/revoke/revoke_all_for_user/purge_expired`, `passwords.verify_password/hash_password/validate_password_strength`.
- Produces:
  - `auth_service.authenticate(db, email, password) -> User` — raises `AppError(INVALID_CREDENTIALS)` or `AppError(ACCOUNT_LOCKED)`
  - `auth_service.change_password(db, user, current, new) -> None`
  - `POST /auth/login` → `{"token": str, "user": UserRead}`
  - `POST /auth/logout` → 204
  - `GET /auth/me` → `UserRead`
  - `POST /auth/change-password` → 204
  - `UserRead` schema: `id`, `email`, `full_name`, `role`, `is_active`, `must_change_password`, `last_login_at`, `created_at`

- [ ] **Step 1: Add the error codes**

In `app/errors.py`, in `ErrorCode` under a new group, and in `HTTP_STATUS_BY_CODE`:

```python
    # --- 401 / 403: the *caller* is the problem ---------------------------
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
```

```python
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.NOT_AUTHENTICATED: 401,
    ErrorCode.ACCOUNT_LOCKED: 403,
    ErrorCode.PASSWORD_CHANGE_REQUIRED: 403,
    ErrorCode.FORBIDDEN: 403,
```

`INVALID_CREDENTIALS` and `NOT_AUTHENTICATED` are distinct: the first means
"those details are wrong", the second "you sent no session". A client needs to
tell them apart to decide between showing an error and redirecting to login.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_auth_api.py`:

```python
"""Logging in, out, and who am I."""

from __future__ import annotations

from datetime import timedelta

from app.db.app_state import get_sessionmaker
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User
from app.security.passwords import hash_password

PASSWORD = "a-perfectly-fine-password"


def make_user(**over) -> User:
    """Insert a user directly. There is no admin API yet to create one with."""
    db = get_sessionmaker()()
    try:
        user = User(
            email=over.pop("email", "analyst@b.test"),
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


def login(client, email="analyst@b.test", password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_a_correct_password_returns_a_token_and_the_user(client, app_db):
    make_user()

    response = login(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"]
    assert body["user"]["email"] == "analyst@b.test"
    assert body["user"]["role"] == "analyst"


def test_the_password_hash_never_appears_in_a_response(client, app_db):
    make_user()

    body = login(client).json()

    assert "password_hash" not in body["user"]
    assert "password" not in body["user"]


def test_email_is_matched_without_regard_to_case(client, app_db):
    """Somebody typing their address with a capital is not a failed login."""
    make_user(email="analyst@b.test")

    assert login(client, email="Analyst@B.test").status_code == 200


def test_a_wrong_password_is_refused(client, app_db):
    make_user()

    response = login(client, password="wrong-but-long-enough")

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


def test_an_unknown_email_gives_the_same_answer_as_a_wrong_password(client, app_db):
    """Otherwise the login form is an oracle for which addresses have accounts,
    which is the first thing an attacker enumerates."""
    make_user()

    unknown = login(client, email="nobody@b.test")
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
    assert response.json()["email"] == "analyst@b.test"


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
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_auth_api.py -v`
Expected: FAIL — 404 on `/auth/login`, since the router does not exist.

- [ ] **Step 4: Write the schemas**

Create `app/schemas/auth.py`:

```python
"""Request and response bodies for authentication.

``UserRead`` deliberately does not declare ``password_hash``. Credentials
cannot leak through a response model that has no field for them, which is the
same discipline ``ConnectionRead`` uses for target-database passwords.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import UserRole
from app.schemas.common import UtcDatetime


class LoginRequest(BaseModel):
    email: EmailStr
    # No length rule here. The policy applies to passwords being *set*, and
    # enforcing it on login would reject a valid old password after the policy
    # tightened, locking out the very people who complied with the old one.
    password: str = Field(min_length=1, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: UtcDatetime | None
    created_at: UtcDatetime


class LoginResponse(BaseModel):
    token: str
    user: UserRead


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)
```

If `app/schemas/common.py` does not export `UtcDatetime`, import it from
wherever `app/schemas/query.py` imports it — that module already uses the type.

`EmailStr` requires `email-validator`. Add it if the import fails:
`uv add email-validator`.

- [ ] **Step 5: Write the service**

Create `app/services/auth_service.py`:

```python
"""Turning credentials into a user, and changing a password.

Every failure path returns the same error with the same message. An
authentication endpoint that distinguishes "no such account" from "wrong
password" is an oracle for which email addresses are registered, and that list
is the first thing an attacker builds.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.models.base import utcnow
from app.models.user import User
from app.security import passwords
from app.services import session_service

#: Failures before an account is locked. Low enough to stop a guessing run,
#: high enough to survive a person mistyping their own password twice.
MAX_FAILED_LOGINS = 5

#: How long a lock lasts. Long enough to make an online attack pointless,
#: short enough that a locked-out colleague is not blocked for the afternoon.
LOCKOUT_MINUTES = 15

#: One message for every failure. Assigned once so the two raise sites cannot
#: drift apart and start distinguishing themselves by wording.
_REFUSAL = "Those details are not right."


def authenticate(db: Session, email: str, password: str) -> User:
    """The user behind these credentials, or raise.

    Raises ``ACCOUNT_LOCKED`` only for an account that is genuinely locked and
    whose password was otherwise correct-shaped; every other failure raises
    ``INVALID_CREDENTIALS``.
    """
    now = utcnow()
    normalised = email.strip().lower()

    user = db.scalar(select(User).where(func.lower(User.email) == normalised))

    if user is None:
        # Hash anyway. Returning immediately makes an unknown address answer
        # measurably faster than a known one, which is the same oracle the
        # shared message exists to close.
        passwords.verify_password(password, passwords.hash_password("timing-equaliser"))
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    if user.locked_until is not None and user.locked_until > now:
        raise AppError(
            ErrorCode.ACCOUNT_LOCKED,
            f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.",
        )

    if not passwords.verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
        db.commit()
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    # Deactivated accounts fail here rather than earlier, and with the same
    # message: "this account is switched off" confirms the address is real.
    if not user.is_active:
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    if (
        user.temp_password_expires_at is not None
        and user.temp_password_expires_at <= now
    ):
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _REFUSAL)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()

    # Opportunistic, on the one operation that is already slow because it
    # verifies a password. Cheaper than a scheduler that can stop running
    # without anybody noticing.
    session_service.purge_expired(db)
    return user


def change_password(db: Session, user: User, current: str, new: str) -> None:
    """Replace a password, then sign the account out everywhere else."""
    if not passwords.verify_password(current, user.password_hash):
        raise AppError(ErrorCode.INVALID_CREDENTIALS, "That is not your current password.")

    passwords.validate_password_strength(new)

    user.password_hash = passwords.hash_password(new)
    user.must_change_password = False
    user.temp_password_expires_at = None
    db.commit()
```

- [ ] **Step 6: Write the router**

Create `app/routers/auth.py`:

```python
"""Logging in, logging out, and reporting who is signed in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.errors import AppError, ErrorCode
from app.models.user import User
from app.schemas.auth import ChangePasswordRequest, LoginRequest, LoginResponse, UserRead
from app.services import auth_service, session_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def current_user(
    request: Request, db: Session = Depends(get_session)
) -> User:
    """The signed-in user, or 401.

    Lives here rather than in the dependency module because ``/auth/me`` and
    ``/auth/logout`` need it before the general-purpose dependencies exist, and
    Task 5 re-exports it rather than writing a second copy.
    """
    user = session_service.resolve(db, _bearer(request))
    if user is None:
        raise AppError(ErrorCode.NOT_AUTHENTICATED, "Sign in to continue.")
    return user


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest, request: Request, db: Session = Depends(get_session)
) -> LoginResponse:
    user = auth_service.authenticate(db, body.email, body.password)
    token = session_service.issue(
        db,
        user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return LoginResponse(token=token, user=UserRead.model_validate(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: Session = Depends(get_session)) -> Response:
    # Deliberately not behind ``current_user``: logging out with an already-dead
    # session must succeed, or a user whose session expired cannot clear it.
    session_service.revoke(db, _bearer(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(current_user)) -> UserRead:
    return UserRead.model_validate(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    auth_service.change_password(db, user, body.current_password, body.new_password)

    # Every session but this one. Being signed out of the browser you just used
    # to change your password is a bug; leaving an intercepted session alive is
    # a hole.
    mine = session_service.digest(_bearer(request))
    session_service.revoke_all_for_user(db, user.id)
    session_service.issue_with_id(db, user, mine)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 7: Add the helper the router needs**

The router above calls `session_service.issue_with_id`, which does not exist
yet. Add it to `app/services/session_service.py`:

```python
def issue_with_id(db: Session, user: User, session_id: str) -> None:
    """Recreate a session under a digest the caller already holds.

    Used only by the password change, which revokes every session for the user
    and then restores the one that made the request. Restoring is simpler and
    less error-prone than a "revoke all except this one" query that has to be
    kept in step with the revocation rules.
    """
    settings = get_settings()
    now = utcnow()
    db.add(
        UserSession(
            id=session_id,
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(hours=settings.session_absolute_hours),
            last_seen_at=now,
        )
    )
    db.commit()
```

- [ ] **Step 8: Wire the router**

In `app/main.py`, beside the other `include_router` calls, and *before* them so
it reads first:

```python
app.include_router(auth.router)
```

Add `auth` to the router imports at the top of the file.

- [ ] **Step 9: Run and watch it pass**

Run: `uv run pytest tests/test_auth_api.py -v`
Expected: PASS, 18 tests.

- [ ] **Step 10: Regenerate the contract**

```bash
uv run python ../../scripts/export_openapi.py
```

- [ ] **Step 11: Commit**

```bash
git add app/routers/auth.py app/services/auth_service.py app/schemas/auth.py \
        app/services/session_service.py app/errors.py app/main.py \
        tests/test_auth_api.py ../../contracts/openapi.json pyproject.toml uv.lock
git commit -m "feat(auth): login, logout, and the current user"
```

---

### Task 5: The dependencies, and a test that no route escapes them

**Files:**
- Create: `app/security/deps.py`
- Create: `tests/test_route_coverage.py`
- Modify: every module in `app/routers/`
- Test: `tests/test_role_enforcement.py`

**Interfaces:**
- Consumes: `current_user` from `app/routers/auth.py`.
- Produces:
  - `require_user(user: User = Depends(current_user)) -> User` — also enforces the password-change gate
  - `require_admin(user: User = Depends(require_user)) -> User`
  - `PUBLIC_PATHS: frozenset[str]` — the allowlist the coverage test reads

- [ ] **Step 1: Write the failing coverage test**

Create `tests/test_route_coverage.py`:

```python
"""No route reaches the database without a signed-in user behind it.

This is the highest-value test in the authentication work, because it is the
one that stays correct as the app grows. Every other test here checks a rule
that exists today; this one fails when somebody adds an endpoint next year and
forgets the dependency. A hole introduced that way is invisible in review - the
new code looks exactly like the code beside it.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import app
from app.security.deps import PUBLIC_PATHS, require_admin, require_user


def _dependency_functions(route: APIRoute) -> set:
    return {d.call for d in route.dependant.dependencies}


def _guarded_routes() -> list[APIRoute]:
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in PUBLIC_PATHS
    ]


def test_there_are_routes_to_check():
    """Guards the guard. If the collection breaks and returns nothing, every
    assertion below passes vacuously and this file becomes decoration."""
    assert len(_guarded_routes()) > 15


@pytest.mark.parametrize("route", _guarded_routes(), ids=lambda r: f"{r.path}")
def test_every_route_requires_a_signed_in_user(route):
    functions = _dependency_functions(route)
    assert require_user in functions or require_admin in functions, (
        f"{route.path} has no authentication dependency. Add require_user or "
        f"require_admin to its router, or add the path to PUBLIC_PATHS with a "
        f"comment explaining why it is safe to expose."
    )


def test_the_public_allowlist_stays_small():
    """Every entry is a decision. Growth here should be noticed."""
    assert PUBLIC_PATHS == frozenset({"/health", "/auth/login", "/auth/logout"})
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_route_coverage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.security.deps'`

- [ ] **Step 3: Write the dependencies**

Create `app/security/deps.py`:

```python
"""The dependencies every router hangs its authorisation on.

Applied at the router rather than the endpoint. An endpoint-level dependency is
a thing somebody has to remember on the day they add a route; a router-level one
is inherited by construction, and ``tests/test_route_coverage.py`` fails the
build if a router ever ships without one.
"""

from __future__ import annotations

from fastapi import Depends

from app.errors import AppError, ErrorCode
from app.models.user import User
from app.routers.auth import current_user

#: Routes that answer without a session, each one a deliberate decision.
#:
#: ``/health`` so a load balancer can probe without credentials. ``/auth/login``
#: because it is where credentials are exchanged. ``/auth/logout`` because
#: clearing an already-expired session must succeed rather than 401.
PUBLIC_PATHS = frozenset({"/health", "/auth/login", "/auth/logout"})


def require_user(user: User = Depends(current_user)) -> User:
    """A signed-in, active user who has finished setting up their account.

    The password-change gate lives here rather than in each router because a
    restricted session that can still call the API is not restricted. The
    frontend declining to show other screens is presentation, not a control.
    """
    if user.must_change_password:
        raise AppError(
            ErrorCode.PASSWORD_CHANGE_REQUIRED,
            "Choose a new password before continuing.",
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """An administrator.

    The message says what is required rather than merely refusing, so a person
    who has been given the wrong role finds out why instead of filing a bug.
    """
    if not user.is_admin:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "This needs an administrator account.",
        )
    return user
```

- [ ] **Step 4: Attach the dependency to every existing router**

For each of `connections.py`, `dashboards.py`, `introspection.py`, `queries.py`,
`flag_rules.py`, add `dependencies=[Depends(require_user)]` to every
`APIRouter(...)` construction in the file. For example, in
`app/routers/dashboards.py`:

```python
from fastapi import APIRouter, Depends, Response, status

from app.security.deps import require_user

router = APIRouter(
    prefix="/dashboards",
    tags=["dashboards"],
    dependencies=[Depends(require_user)],
)
```

`queries.py` and `flag_rules.py` each construct several routers
(`connection_scoped`, `query_scoped`, `summary_scoped`). Every one needs it.

Connections are admin-managed, but analysts must still *list* them, so
`connections.py` takes `require_user` at the router level; the write endpoints
get `require_admin` in the next step.

- [ ] **Step 5: Run the coverage test**

Run: `uv run pytest tests/test_route_coverage.py -v`
Expected: PASS. If a route fails, the assertion message names its path.

- [ ] **Step 6: Write the role-enforcement test**

Create `tests/test_role_enforcement.py`:

```python
"""Analysts are refused the endpoints that manage the system."""

from __future__ import annotations

import pytest

from app.models.enums import UserRole
from tests.test_auth_api import login, make_user

PASSWORD = "a-perfectly-fine-password"


@pytest.fixture
def analyst_auth(client, app_db):
    make_user(email="analyst@b.test", role=UserRole.ANALYST)
    token = login(client, email="analyst@b.test").json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_auth(client, app_db):
    make_user(email="admin@b.test", role=UserRole.ADMIN)
    token = login(client, email="admin@b.test").json()["token"]
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


def test_an_unauthenticated_caller_gets_401_not_403(client, app_db):
    """The two are different answers to different questions: 401 means "sign
    in", 403 means "signed in, still not allowed". A client that conflates them
    either redirects a permitted user to login or shows an access error to
    somebody who simply needs to sign in."""
    assert client.get("/connections").status_code == 401
```

- [ ] **Step 7: Add `require_admin` to the connection write endpoints**

In `app/routers/connections.py`, add `dependencies=[Depends(require_admin)]` to
each of the create, update, delete, test and pause endpoint decorators. The
router-level `require_user` still applies to the read endpoints.

- [ ] **Step 8: Run both tests**

Run: `uv run pytest tests/test_route_coverage.py tests/test_role_enforcement.py -v`
Expected: PASS.

- [ ] **Step 9: Repair the existing suite**

Every existing API test now calls unauthenticated endpoints and gets 401. Add an
autouse fixture to `tests/conftest.py`:

```python
@pytest.fixture
def admin_client(client, app_db):
    """A TestClient carrying an admin session.

    Existing tests predate authentication and assert behaviour rather than
    permissions, so they run as an admin. The permission rules themselves are
    covered by tests/test_role_enforcement.py, which uses both roles
    deliberately.
    """
    from tests.test_auth_api import login, make_user
    from app.models.enums import UserRole

    make_user(email="suite-admin@b.test", role=UserRole.ADMIN)
    token = login(client, email="suite-admin@b.test").json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
```

Then replace the `client` fixture argument with `admin_client` in every existing
API test module. Run the full suite between each module so a failure names one
file rather than twenty.

- [ ] **Step 10: Run everything**

Run: `uv run pytest -m "not integration and not slow"`
Expected: PASS, all tests.

- [ ] **Step 11: Commit**

```bash
git add app/security/deps.py app/routers/ tests/
git commit -m "feat(auth): require a session on every route, and prove it"
```

---

### Task 6: The CLI

**Files:**
- Create: `app/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `passwords`, `User`, `UserRole`, `get_sessionmaker`.
- Produces: console script `fae` with `create-admin`, `reset-password`, `list-users`.

- [ ] **Step 1: Add Typer and the entry point**

```bash
uv add typer
```

In `pyproject.toml`:

```toml
[project.scripts]
fae = "app.cli:app"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_cli.py`:

```python
"""The command that creates the first administrator."""

from __future__ import annotations

from typer.testing import CliRunner

from app.cli import app as cli
from app.db.app_state import get_sessionmaker
from app.models.enums import UserRole
from app.models.user import User

runner = CliRunner()


def test_create_admin_makes_an_active_administrator(app_db):
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert result.exit_code == 0, result.output
    db = get_sessionmaker()()
    try:
        user = db.query(User).one()
        assert user.email == "boss@b.test"
        assert user.role is UserRole.ADMIN
        assert user.is_active is True
    finally:
        db.close()


def test_the_first_admin_is_not_asked_to_change_its_password(app_db):
    """They chose it themselves at the prompt, so there is nothing to replace -
    and an admin locked into a change screen at first login with nobody able to
    reset them is a bootstrap that fails at the last step."""
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    db = get_sessionmaker()()
    try:
        assert db.query(User).one().must_change_password is False
    finally:
        db.close()


def test_the_password_is_never_written_in_the_clear(app_db):
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    db = get_sessionmaker()()
    try:
        assert "a-perfectly-fine-password" not in db.query(User).one().password_hash
    finally:
        db.close()


def test_it_prints_the_database_it_is_writing_to(app_db):
    """Run from the wrong directory the command would otherwise create an admin
    in the SQLite fallback while the real Postgres stayed empty, and the only
    symptom would be "invalid credentials" at a login page."""
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert "sqlite" in result.output.lower() or "postgres" in result.output.lower()


def test_a_mistyped_confirmation_creates_nobody(app_db):
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-different-password\n",
    )

    assert result.exit_code != 0
    db = get_sessionmaker()()
    try:
        assert db.query(User).count() == 0
    finally:
        db.close()


def test_a_weak_password_creates_nobody(app_db):
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="short\nshort\n",
    )

    assert result.exit_code != 0
    db = get_sessionmaker()()
    try:
        assert db.query(User).count() == 0
    finally:
        db.close()


def test_a_duplicate_email_is_refused(app_db):
    for _ in range(2):
        result = runner.invoke(
            cli,
            ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
            input="a-perfectly-fine-password\na-perfectly-fine-password\n",
        )

    assert result.exit_code != 0
    assert "already" in result.output.lower()


def test_reset_password_issues_a_temporary_one(app_db):
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    result = runner.invoke(cli, ["reset-password", "--email", "boss@b.test"])

    assert result.exit_code == 0, result.output
    db = get_sessionmaker()()
    try:
        user = db.query(User).one()
        assert user.must_change_password is True
        assert user.temp_password_expires_at is not None
    finally:
        db.close()


def test_reset_password_prints_the_temporary_password_once(app_db):
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    result = runner.invoke(cli, ["reset-password", "--email", "boss@b.test"])

    # 16 characters from the unambiguous alphabet, printed for the operator to
    # convey. There is no other way to learn it.
    assert "shown once" in result.output.lower()


def test_reset_password_refuses_an_unknown_account(app_db):
    result = runner.invoke(cli, ["reset-password", "--email", "nobody@b.test"])

    assert result.exit_code != 0


def test_list_users_shows_role_and_state(app_db):
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    result = runner.invoke(cli, ["list-users"])

    assert "boss@b.test" in result.output
    assert "admin" in result.output
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.cli'`

- [ ] **Step 4: Implement**

Create `app/cli.py`:

```python
"""Operator commands that need no running server and no signed-in user.

The first administrator is created here and nowhere else. An HTTP endpoint that
mints an administrator is reachable by anything that can reach the service; a
command is reachable by somebody who can already read the database, which is the
bar this is meant to sit at.
"""

from __future__ import annotations

from datetime import timedelta

import typer
from sqlalchemy import func, inspect, select

from app.config import get_settings
from app.db.app_state import get_engine, get_sessionmaker
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User
from app.security import passwords

app = typer.Typer(help="Switchboard operator commands.", no_args_is_help=True)

#: How long an issued temporary password stays usable. An unclaimed credential
#: valid forever in somebody's chat history is a standing liability.
TEMP_PASSWORD_HOURS = 72


def _target_description() -> str:
    """The database about to be written to, with any password removed."""
    url = get_engine().url
    return url.render_as_string(hide_password=True)


def _require_schema() -> None:
    """Refuse to run before migrations have.

    Without this, running from the wrong directory creates an administrator in
    the SQLite fallback while the real database stays empty - and the only
    symptom is "invalid credentials" at a login page, with nothing anywhere
    explaining why.
    """
    if "users" not in inspect(get_engine()).get_table_names():
        typer.secho(
            f"No users table in {_target_description()}.\n"
            "Run the migrations first, and check you are in services/analyzer.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)


@app.command("create-admin")
def create_admin(
    email: str = typer.Option(..., prompt=True),
    name: str = typer.Option(..., prompt="Full name"),
) -> None:
    """Create the first administrator."""
    _require_schema()
    typer.echo(f"Target database: {_target_description()}")

    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    try:
        passwords.validate_password_strength(password)
    except Exception as error:  # AppError carries the reason
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    normalised = email.strip().lower()
    db = get_sessionmaker()()
    try:
        existing = db.scalar(select(User).where(func.lower(User.email) == normalised))
        if existing is not None:
            typer.secho(f"{normalised} already has an account.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        db.add(
            User(
                email=normalised,
                full_name=name.strip(),
                password_hash=passwords.hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
                # They chose it at the prompt, so there is nothing to replace.
                # Forcing a change here would trap the first admin behind a
                # screen with nobody able to reset them.
                must_change_password=False,
            )
        )
        db.commit()
    finally:
        db.close()

    typer.secho(f"Administrator created: {normalised}", fg=typer.colors.GREEN)


@app.command("reset-password")
def reset_password(email: str = typer.Option(..., prompt=True)) -> None:
    """Issue a temporary password for an account that is locked out."""
    _require_schema()
    normalised = email.strip().lower()

    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(func.lower(User.email) == normalised))
        if user is None:
            typer.secho(f"No account for {normalised}.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        temporary = passwords.generate_temporary_password()
        user.password_hash = passwords.hash_password(temporary)
        user.must_change_password = True
        user.temp_password_expires_at = utcnow() + timedelta(hours=TEMP_PASSWORD_HOURS)
        user.failed_login_count = 0
        user.locked_until = None
        db.commit()

        from app.services import session_service

        session_service.revoke_all_for_user(db, user.id)
    finally:
        db.close()

    typer.echo("")
    typer.secho(f"Temporary password: {temporary}", fg=typer.colors.YELLOW, bold=True)
    typer.echo(
        f"Shown once, and only here. Valid for {TEMP_PASSWORD_HOURS} hours; "
        "they must choose a new password at first sign-in."
    )


@app.command("list-users")
def list_users() -> None:
    """Every account, with its role and whether it is switched on."""
    _require_schema()
    db = get_sessionmaker()()
    try:
        users = db.scalars(select(User).order_by(User.email)).all()
        if not users:
            typer.echo("No accounts yet. Run: fae create-admin")
            return
        for user in users:
            state = "active" if user.is_active else "inactive"
            typer.echo(f"{user.email:40} {user.role.value:8} {state}")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover - console script is the entry point
    app()
```

- [ ] **Step 5: Run and watch it pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 6: Try it by hand**

```bash
uv run fae list-users
```
Expected: either "No accounts yet" or a list. Confirms the console script is
installed and reaches the configured database.

- [ ] **Step 7: Commit**

```bash
git add app/cli.py tests/test_cli.py pyproject.toml uv.lock
git commit -m "feat(auth): fae create-admin, reset-password and list-users"
```

---

### Task 7: Ownership

**Files:**
- Create: `alembic/versions/0012_ownership.py`
- Modify: `app/models/saved_query.py`, `app/models/dashboard.py`, `app/models/connection.py`, `app/models/execution_log.py`
- Modify: `app/services/saved_query_service.py`, `app/services/dashboard_service.py`
- Modify: `app/routers/queries.py`, `app/routers/dashboards.py`
- Test: `tests/test_ownership.py`

**Interfaces:**
- Consumes: `require_user` from `app/security/deps.py`.
- Produces:
  - `owner_id: Mapped[str | None]` on `SavedQuery` and `Dashboard`
  - `created_by: Mapped[str | None]` on `Connection`
  - `user_id: Mapped[str | None]` on `ExecutionLog`
  - `saved_query_service.visible_to(user) -> ColumnElement[bool]` — the filter clause an analyst may see through
  - `saved_query_service.get_owned(db, query_id, user)` — raises `QUERY_NOT_FOUND` for somebody else's

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ownership.py`:

```python
"""An analyst sees their own work and nobody else's."""

from __future__ import annotations

import pytest

from app.models.enums import UserRole
from tests.test_auth_api import login, make_user

PASSWORD = "a-perfectly-fine-password"


def _auth(client, email, role):
    make_user(email=email, role=role)
    return {"Authorization": f"Bearer {login(client, email=email).json()['token']}"}


@pytest.fixture
def alice(client, app_db):
    return _auth(client, "alice@b.test", UserRole.ANALYST)


@pytest.fixture
def bob(client, app_db):
    return _auth(client, "bob@b.test", UserRole.ANALYST)


@pytest.fixture
def boss(client, app_db):
    return _auth(client, "boss@b.test", UserRole.ADMIN)


@pytest.fixture
def connection(client, boss, target_sqlite):
    return client.post(
        "/connections",
        headers=boss,
        json={"name": "shared", "db_type": "sqlite", "sqlite_path": target_sqlite},
    ).json()["connection"]


def _query(client, auth, connection, name):
    response = client.post(
        f"/connections/{connection['id']}/queries",
        headers=auth,
        json={"name": name, "sql_text": "SELECT day, count(*) AS n FROM txns GROUP BY day"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_query_belongs_to_whoever_created_it(client, alice, connection):
    created = _query(client, alice, connection, "alice's")

    listed = client.get("/queries", headers=alice).json()
    assert [q["id"] for q in listed] == [created["id"]]


def test_an_analyst_does_not_see_another_analysts_queries(client, alice, bob, connection):
    _query(client, alice, connection, "alice's")

    assert client.get("/queries", headers=bob).json() == []


def test_an_analyst_cannot_fetch_another_analysts_query_by_id(client, alice, bob, connection):
    """Absence from a list is not protection: the id is guessable from a URL
    somebody pasted into chat."""
    created = _query(client, alice, connection, "alice's")

    response = client.get(f"/queries/{created['id']}", headers=bob)

    # 404, not 403. Confirming that an id exists but belongs to somebody else
    # tells Bob what Alice is working on.
    assert response.status_code == 404


def test_an_analyst_cannot_edit_another_analysts_query(client, alice, bob, connection):
    created = _query(client, alice, connection, "alice's")

    response = client.put(
        f"/queries/{created['id']}",
        headers=bob,
        json={"name": "bob's now"},
    )

    assert response.status_code == 404


def test_an_analyst_cannot_delete_another_analysts_query(client, alice, bob, connection):
    created = _query(client, alice, connection, "alice's")

    assert client.delete(f"/queries/{created['id']}", headers=bob).status_code == 404


def test_an_analyst_cannot_reach_another_analysts_charts(client, alice, bob, connection):
    created = _query(client, alice, connection, "alice's")

    response = client.get(f"/queries/{created['id']}/charts", headers=bob)

    assert response.status_code == 404


def test_an_analyst_cannot_run_another_analysts_query(client, alice, bob, connection):
    """The one that actually touches the customer's database."""
    created = _query(client, alice, connection, "alice's")

    assert client.get(f"/queries/{created['id']}/poll", headers=bob).status_code == 404


def test_an_admin_sees_every_analysts_work(client, alice, bob, boss, connection):
    _query(client, alice, connection, "alice's")
    _query(client, bob, connection, "bob's")

    listed = client.get("/queries", headers=boss).json()

    assert len(listed) == 2


def test_an_admin_can_open_an_analysts_query(client, alice, boss, connection):
    created = _query(client, alice, connection, "alice's")

    assert client.get(f"/queries/{created['id']}", headers=boss).status_code == 200


def test_running_a_query_records_who_ran_it(client, alice, connection):
    """The audit question this whole change exists to answer."""
    created = _query(client, alice, connection, "alice's")
    client.get(f"/queries/{created['id']}/poll?force=true", headers=alice)

    logs = client.get(f"/queries/{created['id']}/logs", headers=alice).json()

    assert logs
    assert logs[0]["user_id"] is not None


def test_dashboards_belong_to_their_creator(client, alice, bob):
    created = client.post("/dashboards", headers=alice, json={"name": "alice's board"})
    assert created.status_code == 201, created.text

    assert client.get("/dashboards", headers=bob).json() == []
    assert len(client.get("/dashboards", headers=alice).json()) == 1


def test_unowned_rows_are_invisible_to_analysts(client, alice, boss, connection, app_db):
    """Everything created before accounts existed has no owner. It must not
    become visible to whoever signs up first."""
    from app.db.app_state import get_sessionmaker
    from app.models.saved_query import SavedQuery

    db = get_sessionmaker()()
    try:
        db.add(
            SavedQuery(
                connection_id=connection["id"],
                name="legacy",
                sql_text="SELECT 1 AS n",
                owner_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    assert client.get("/queries", headers=alice).json() == []
    assert len(client.get("/queries", headers=boss).json()) == 1
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_ownership.py -v`
Expected: FAIL — `owner_id` is not a column on `SavedQuery`.

- [ ] **Step 3: Add the columns to the models**

In `app/models/saved_query.py`, inside `class SavedQuery`:

```python
    #: Who created this. Nullable because rows predating accounts have no
    #: owner, and because an owner is never deleted so the column never has to
    #: be cleared. An unowned row is visible to administrators only.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
```

The same block in `app/models/dashboard.py` on `class Dashboard`.

In `app/models/connection.py` on `class Connection`:

```python
    #: The administrator who added this database.
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
```

In `app/models/execution_log.py` on the log model:

```python
    #: Who caused this run. Null for a scheduled or background refresh, which
    #: nobody asked for interactively.
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
```

Add `ForeignKey` to the SQLAlchemy imports in any file that does not already
import it.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0012_ownership.py`:

```python
"""Who owns what.

Revision ID: 0012_ownership
Revises: 0011_users_and_sessions
Create Date: 2026-08-26

Every column is nullable and nothing is backfilled. There is no administrator
at migration time to attribute existing rows to, and inventing one would be
worse than leaving them unowned: an unowned row is visible to administrators
only, which is the safe default, whereas a guessed owner would hand somebody
else's work to whoever happened to be created first.

ON DELETE RESTRICT rather than CASCADE or SET NULL. Accounts are deactivated,
never deleted, and the schema should refuse the deletion rather than quietly
destroy an investigation's queries or orphan them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_ownership"
down_revision = "0011_users_and_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("saved_queries") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_saved_queries_owner", "users", ["owner_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index("ix_saved_queries_owner_id", "saved_queries", ["owner_id"])

    with op.batch_alter_table("dashboards") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_dashboards_owner", "users", ["owner_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index("ix_dashboards_owner_id", "dashboards", ["owner_id"])

    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("created_by", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_connections_created_by", "users", ["created_by"], ["id"], ondelete="RESTRICT"
        )

    with op.batch_alter_table("execution_logs") as batch:
        batch.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_execution_logs_user", "users", ["user_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_index("ix_execution_logs_user_id", "execution_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_execution_logs_user_id", table_name="execution_logs")
    with op.batch_alter_table("execution_logs") as batch:
        batch.drop_constraint("fk_execution_logs_user", type_="foreignkey")
        batch.drop_column("user_id")

    with op.batch_alter_table("connections") as batch:
        batch.drop_constraint("fk_connections_created_by", type_="foreignkey")
        batch.drop_column("created_by")

    op.drop_index("ix_dashboards_owner_id", table_name="dashboards")
    with op.batch_alter_table("dashboards") as batch:
        batch.drop_constraint("fk_dashboards_owner", type_="foreignkey")
        batch.drop_column("owner_id")

    op.drop_index("ix_saved_queries_owner_id", table_name="saved_queries")
    with op.batch_alter_table("saved_queries") as batch:
        batch.drop_constraint("fk_saved_queries_owner", type_="foreignkey")
        batch.drop_column("owner_id")
```

Confirm the execution-log table name before running: check
`app/models/execution_log.py` for `__tablename__` and use exactly that string.

- [ ] **Step 5: Add the visibility helpers to the service**

In `app/services/saved_query_service.py`:

```python
def visible_to(user: User):
    """The filter clause deciding which queries a caller may see.

    Returns a clause rather than a query so every call site composes it into
    whatever it was already selecting, instead of each one re-deriving the rule.

    An administrator sees everything, including unowned rows. An analyst sees
    only rows they own - never unowned ones, which belong to the era before
    accounts and must not fall to whoever signs in first.
    """
    if user.is_admin:
        return sa.true()
    return SavedQuery.owner_id == user.id


def get_owned(db: Session, query_id: str, user: User) -> SavedQuery:
    """One query the caller is entitled to, or raise QUERY_NOT_FOUND.

    Not found rather than forbidden, deliberately. Confirming that an id exists
    but belongs to somebody else tells an analyst what a colleague is working
    on, and the id is guessable from any URL that has been pasted into a chat.
    """
    query = db.get(SavedQuery, query_id)
    if query is None:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such query.")
    if not user.is_admin and query.owner_id != user.id:
        raise AppError(ErrorCode.QUERY_NOT_FOUND, "No such query.")
    return query
```

Import `sqlalchemy as sa` and `User` at the top of that module.

Add the equivalent pair to `app/services/dashboard_service.py` against
`Dashboard` and `DASHBOARD_NOT_FOUND`.

- [ ] **Step 6: Use them in the routers**

In `app/routers/queries.py`, add `user: User = Depends(require_user)` to every
endpoint signature, then:

- the create endpoint sets `owner_id=user.id` on the new `SavedQuery`
- the list endpoint adds `.where(svc.visible_to(user))`
- every endpoint taking a `query_id` replaces its `db.get(SavedQuery, ...)`
  lookup with `svc.get_owned(db, query_id, user)`
- the run and poll endpoints pass `user.id` into `log_execution`

The same treatment in `app/routers/dashboards.py` and in
`app/routers/flag_rules.py`, which reaches its query through `get_owned` too.

In `app/routers/connections.py`, the create endpoint sets
`created_by=user.id`.

In `app/services/saved_query_service.log_execution`, add a
`user_id: str | None = None` parameter and set it on the row. Background
refreshes in `app/services/refresher.py` and `app/services/scheduler.py` call
it without a user, which is correct: nobody asked for those interactively.

- [ ] **Step 7: Run the ownership tests**

Run: `uv run pytest tests/test_ownership.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 8: Run everything**

Run: `uv run pytest -m "not integration and not slow"`
Expected: PASS.

- [ ] **Step 9: Regenerate the contract**

```bash
uv run python ../../scripts/export_openapi.py
```

- [ ] **Step 10: Commit**

```bash
git add app/models/ app/services/ app/routers/ alembic/versions/0012_ownership.py \
        tests/test_ownership.py ../../contracts/openapi.json
git commit -m "feat(auth): queries and dashboards belong to whoever built them"
```

---

### Task 8: The forced password change

**Files:**
- Test: `tests/test_password_change_gate.py`
- Modify: none — the gate is already in `require_user` from Task 5

**Interfaces:**
- Consumes: `require_user`, `PASSWORD_CHANGE_REQUIRED`.
- Produces: no new symbols. This task proves behaviour that Task 5 built.

This is a whole task rather than a step because it is the one rule enforced
across every endpoint at once, and a reviewer should be able to reject it
independently.

- [ ] **Step 1: Write the tests**

Create `tests/test_password_change_gate.py`:

```python
"""An account holding a temporary password can do exactly one thing."""

from __future__ import annotations

from datetime import timedelta

from app.db.app_state import get_sessionmaker
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User
from tests.test_auth_api import login, make_user

TEMPORARY = "a-temporary-password"


def _temp_user(email="new@b.test", role=UserRole.ANALYST):
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

    assert login(client, email="new@b.test", password=TEMPORARY).status_code == 200


def test_every_other_endpoint_is_refused(client, app_db):
    _temp_user()
    token = login(client, email="new@b.test", password=TEMPORARY).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    response = client.get("/connections", headers=auth)

    assert response.status_code == 403
    assert response.json()["error_code"] == "PASSWORD_CHANGE_REQUIRED"


def test_the_refusal_applies_to_administrators_too(client, app_db):
    """A reset administrator is not exempt: the point is that the credential
    was issued by somebody else, and role does not change that."""
    _temp_user(email="boss@b.test", role=UserRole.ADMIN)
    token = login(client, email="boss@b.test", password=TEMPORARY).json()["token"]

    response = client.get("/connections", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


def test_the_change_endpoint_itself_is_allowed(client, app_db):
    """The one exception, or the account is bricked."""
    _temp_user()
    token = login(client, email="new@b.test", password=TEMPORARY).json()["token"]

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
    token = login(client, email="new@b.test", password=TEMPORARY).json()["token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True


def test_the_rest_of_the_app_opens_once_the_password_is_changed(client, app_db):
    _temp_user()
    token = login(client, email="new@b.test", password=TEMPORARY).json()["token"]
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

    assert login(client, email="new@b.test", password=TEMPORARY).status_code == 401
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_password_change_gate.py -v`
Expected: PASS if Tasks 4 and 5 are correct. If `/auth/me` is refused, the
`me` endpoint is depending on `require_user` instead of `current_user` —
change it to `current_user`, since the app cannot route to the change screen
without being able to ask who it is talking to.

- [ ] **Step 3: Update `make_user` to accept the flag**

`tests/test_auth_api.make_user` already forwards unknown keyword arguments to
the `User` constructor, so `must_change_password=True` works. Confirm it does;
if not, that is the fix.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -m "not integration and not slow"`
Expected: PASS.

- [ ] **Step 5: Update the documentation**

In `services/analyzer/README.md`, add a short section under the existing setup
instructions:

```markdown
## Accounts

Every endpoint except `/health` and `/auth/login` needs a signed-in user.

Create the first administrator — the only way an admin account comes into
existence, and it needs shell access rather than network access:

    cd services/analyzer
    uv run fae create-admin

Under Docker:

    docker compose exec analyzer uv run fae create-admin

The command prints the database it is about to write to. If that is not the
database you expect, you are in the wrong directory: the settings are read from
`services/analyzer/.env`.

Other commands:

    uv run fae reset-password --email someone@example.com
    uv run fae list-users

`reset-password` prints a temporary password once. It is valid for 72 hours and
the holder must choose a new one before they can use anything else.
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_password_change_gate.py README.md
git commit -m "feat(auth): a temporary password unlocks nothing but the change screen"
```

---

## Merging

Every task above lands on `feat/auth-engine`. The branch merges to master as a
unit: the app is unusable between Tasks 4 and 7, because routes require a user
before ownership exists to scope them by.

Before merging:

```bash
uv run pytest -m "not integration and not slow"
uv run python ../../scripts/export_openapi.py
git diff --exit-code ../../contracts/    # must be clean
```

Then rebuild the container so the running engine has the new schema and CLI:

```bash
cd services/analyzer
docker compose build analyzer && docker compose up -d analyzer
docker compose exec analyzer uv run fae create-admin
```

**The dashboard will be completely broken at this point** — every request it
makes returns 401. That is expected and is what the next plan fixes. Do not
merge this branch to master until the frontend plan is ready to follow it, or
keep both on the same branch.

## What this plan does not cover

Each gets its own plan, written after this one lands so it can be written
against what actually exists rather than what was predicted:

- Admin user-management endpoints and the audit log
- Publishing, and the query freeze it implies
- The Next.js proxy, the login page, route guards, and the admin UI
- The rename to Switchboard and the new identity
- Updating `dev-seed.mjs` and `smoke.mjs`, both of which will fail against an
  authenticated engine
