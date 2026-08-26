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
