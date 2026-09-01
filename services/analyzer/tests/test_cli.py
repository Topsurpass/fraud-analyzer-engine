"""The command that creates the first administrator."""

from __future__ import annotations

from typer.testing import CliRunner

from app.cli import app as cli
from app.db.app_state import get_sessionmaker
from app.models.enums import UserRole
from app.models.connection import Connection
from app.models.enums import DbType
from app.models.saved_query import SavedQuery
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
    symptom would be "invalid credentials" at a login page.

    Asserts the actual target rather than "sqlite or postgres": the
    `isolated_environment` fixture in conftest.py points FAE_APP_DB_URL at a
    per-test SQLite file named app_state.db, so that is the one true answer
    here - an "or" that also accepts the wrong backend's name would pass even
    if this printed the wrong database.
    """
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert "sqlite:///" in result.output
    assert "app_state.db" in result.output


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


# --- The schema guard -------------------------------------------------------
#
# _require_schema() is the one behaviour the brief singles out by name as
# consequence-bearing: without it, running this tool from the wrong directory
# (or before `alembic upgrade head`) mints an administrator into whatever
# database FAE_APP_DB_URL happens to resolve to - often the SQLite fallback -
# while the real database stays untouched. Nothing about that failure is
# loud: the command prints "Administrator created" and exits 0, and the only
# symptom anyone ever sees is "invalid credentials" at a login page, weeks
# later, with nothing anywhere pointing back at this command.
#
# These three deliberately do NOT request the `app_db` fixture. `app_db`
# is what calls `app_state.init_db()` to create the schema; skipping it
# leaves the per-test SQLite file (set up by the autouse
# `isolated_environment` fixture in conftest.py) exactly as fresh migrations
# never touched it - no `users` table, which is the one condition the guard
# exists to catch. That also means the guard fires before any of the
# password machinery runs, so these pay no argon2 cost at all.


def test_create_admin_refuses_when_the_schema_is_missing():
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert result.exit_code != 0
    assert "no users table" in result.output.lower()


def test_reset_password_refuses_when_the_schema_is_missing():
    result = runner.invoke(cli, ["reset-password", "--email", "boss@b.test"])

    assert result.exit_code != 0
    assert "no users table" in result.output.lower()


def test_list_users_refuses_when_the_schema_is_missing():
    result = runner.invoke(cli, ["list-users"])

    assert result.exit_code != 0
    assert "no users table" in result.output.lower()


# --- Claiming what nobody owns ---------------------------------------------
#
# Ownership columns are nullable because nothing predating accounts has an
# owner and there is no administrator at migration time to attribute rows to.
# Nothing else in the application can ever set an owner on those rows, so
# without this step every pre-existing saved query and dashboard stays
# owner_id IS NULL permanently: administrators can see them, the analyst who
# wrote them can never edit them again, and no analyst can see them at all.
# The design names this offer twice - in the data model and in the CLI section.


def _unowned_rows(app_db):
    """One connection, one saved query and one dashboard, all unowned.

    Written straight to the database rather than through the API, because the
    API always sets an owner - which is the point: these rows are the ones
    that existed before there was anybody to own them.
    """
    from app.models import Connection, Dashboard, DbType, SavedQuery

    db = get_sessionmaker()()
    try:
        conn = Connection(name="legacy", db_type=DbType.SQLITE, sqlite_path="/tmp/x.db")
        db.add(conn)
        db.commit()
        db.add_all(
            [
                SavedQuery(connection_id=conn.id, name="legacy q", sql_text="SELECT 1"),
                SavedQuery(connection_id=conn.id, name="older q", sql_text="SELECT 2"),
                Dashboard(name="legacy board"),
            ]
        )
        db.commit()
    finally:
        db.close()


def _owner_ids(model) -> list:
    db = get_sessionmaker()()
    try:
        return [row.owner_id for row in db.query(model).all()]
    finally:
        db.close()


def test_create_admin_reports_what_is_unowned(app_db):
    _unowned_rows(app_db)

    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss", "--claim"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert result.exit_code == 0, result.output
    assert "2 saved queries" in result.output
    assert "1 dashboard" in result.output


def test_create_admin_claims_unowned_work_when_accepted(app_db):
    from app.models import Dashboard, SavedQuery

    _unowned_rows(app_db)

    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss", "--claim"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )
    assert result.exit_code == 0, result.output

    db = get_sessionmaker()()
    try:
        admin_id = db.query(User).one().id
    finally:
        db.close()

    assert _owner_ids(SavedQuery) == [admin_id, admin_id]
    assert _owner_ids(Dashboard) == [admin_id]


def test_create_admin_leaves_unowned_work_alone_when_declined(app_db):
    from app.models import Dashboard, SavedQuery

    _unowned_rows(app_db)

    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss", "--no-claim"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert result.exit_code == 0, result.output
    assert _owner_ids(SavedQuery) == [None, None]
    assert _owner_ids(Dashboard) == [None]


def test_create_admin_asks_when_neither_flag_is_given(app_db):
    """Skippable, and defaulting to claiming: the operator running this is the
    first administrator, and leaving the rows unowned is the outcome nothing
    can undo later."""
    from app.models import Dashboard, SavedQuery

    _unowned_rows(app_db)

    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        # Password, confirmation, then a bare newline accepting the default.
        input="a-perfectly-fine-password\na-perfectly-fine-password\n\n",
    )

    assert result.exit_code == 0, result.output
    db = get_sessionmaker()()
    try:
        admin_id = db.query(User).one().id
    finally:
        db.close()
    assert _owner_ids(SavedQuery) == [admin_id, admin_id]
    assert _owner_ids(Dashboard) == [admin_id]


def test_create_admin_says_nothing_about_claiming_when_there_is_nothing_to_claim(app_db):
    result = runner.invoke(
        cli,
        ["create-admin", "--email", "boss@b.test", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\n",
    )

    assert result.exit_code == 0, result.output
    assert "unowned" not in result.output.lower()


def test_claim_unowned_gives_stranded_work_an_owner(app_db):
    """The situation this exists for.

    Rows created before accounts have no owner, and an unowned row is
    admin-only. A board can then place a card whose query its own owner cannot
    resolve, and the card does not draw for the person who put it there.
    """
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@example.com", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\nn\n",
    )
    db = get_sessionmaker()()
    try:
        conn = Connection(name="legacy target", db_type=DbType.SQLITE, sqlite_path="/tmp/x.db")
        db.add(conn)
        db.flush()
        db.add(
            SavedQuery(
                connection_id=conn.id,
                name="legacy",
                sql_text="SELECT 1 AS n",
                owner_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    result = runner.invoke(cli, ["claim-unowned", "--email", "boss@example.com"], input="y\n")

    assert result.exit_code == 0, result.output
    db = get_sessionmaker()()
    try:
        assert db.query(SavedQuery).one().owner_id is not None
    finally:
        db.close()


def test_claim_unowned_can_be_declined(app_db):
    runner.invoke(
        cli,
        ["create-admin", "--email", "boss@example.com", "--name", "The Boss"],
        input="a-perfectly-fine-password\na-perfectly-fine-password\nn\n",
    )
    db = get_sessionmaker()()
    try:
        conn = Connection(name="legacy target", db_type=DbType.SQLITE, sqlite_path="/tmp/x.db")
        db.add(conn)
        db.flush()
        db.add(
            SavedQuery(
                connection_id=conn.id,
                name="legacy",
                sql_text="SELECT 1 AS n",
                owner_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    runner.invoke(cli, ["claim-unowned", "--email", "boss@example.com"], input="n\n")

    db = get_sessionmaker()()
    try:
        assert db.query(SavedQuery).one().owner_id is None
    finally:
        db.close()


def test_claim_unowned_refuses_an_unknown_account(app_db):
    result = runner.invoke(cli, ["claim-unowned", "--email", "nobody@example.com"])

    assert result.exit_code != 0
