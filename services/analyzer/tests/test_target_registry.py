"""Registry, read-only enforcement, timeouts, and driver-error translation."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import target_registry as reg
from app.errors import (
    DbAuthError,
    DbPermissionError,
    DbUnreachableError,
    ErrorCode,
    InvalidConfigError,
    QueryExecutionError,
    QueryTimeoutError,
)
from app.models import Connection, DbType
from app.security.crypto import encrypt


def sqlite_conn(path: str, cid: str = "c-sqlite") -> Connection:
    c = Connection(name="t", db_type=DbType.SQLITE, sqlite_path=path)
    c.id = cid
    return c


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_build_url_sqlite(target_sqlite):
    url = reg.build_url(sqlite_conn(target_sqlite))
    assert url.drivername == "sqlite"
    assert url.database == target_sqlite


def test_build_url_postgres_decrypts_password_and_defaults_port():
    c = Connection(
        name="pg",
        db_type=DbType.POSTGRES,
        host="db.internal",
        database="fraud",
        username="ro_user",
        password_encrypted=encrypt("s3cret"),
    )
    url = reg.build_url(c)
    assert url.drivername == "postgresql+psycopg"
    assert url.port == 5432
    assert url.password == "s3cret"


def test_build_url_mysql_defaults_port():
    c = Connection(
        name="my", db_type=DbType.MYSQL, host="h", database="d", username="u"
    )
    url = reg.build_url(c)
    assert url.drivername == "mysql+pymysql"
    assert url.port == 3306


def test_build_url_explicit_port_wins():
    c = Connection(
        name="pg", db_type=DbType.POSTGRES, host="h", database="d", username="u", port=6543
    )
    assert reg.build_url(c).port == 6543


def test_build_url_sqlite_without_path_rejected():
    with pytest.raises(InvalidConfigError):
        reg.build_url(Connection(name="x", db_type=DbType.SQLITE))


def test_build_url_postgres_without_host_rejected():
    with pytest.raises(InvalidConfigError):
        reg.build_url(Connection(name="x", db_type=DbType.POSTGRES, database="d"))


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


def test_sqlite_engine_cannot_write(target_sqlite):
    c = sqlite_conn(target_sqlite)
    with pytest.raises(DbPermissionError):
        with reg.read_only_connection(c) as sa:
            sa.execute(text("INSERT INTO txns (day, user_id, amount) VALUES ('x',1,1)"))


def test_sqlite_engine_cannot_create_table(target_sqlite):
    c = sqlite_conn(target_sqlite)
    with pytest.raises(DbPermissionError):
        with reg.read_only_connection(c) as sa:
            sa.execute(text("CREATE TABLE evil (a int)"))


def test_sqlite_engine_can_read(target_sqlite):
    c = sqlite_conn(target_sqlite)
    with reg.read_only_connection(c) as sa:
        assert sa.execute(text("SELECT count(*) FROM txns")).scalar() == 5


def test_probe_succeeds_on_valid_target(target_sqlite):
    reg.probe(sqlite_conn(target_sqlite))


def test_probe_fails_on_missing_file(tmp_path):
    c = sqlite_conn(str(tmp_path / "nope.db"))
    with pytest.raises(DbUnreachableError):
        reg.probe(c)


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_sqlite_runaway_query_times_out(target_sqlite):
    c = sqlite_conn(target_sqlite)
    runaway = text(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r)"
        " SELECT count(*) FROM r"
    )
    with pytest.raises(QueryTimeoutError):
        with reg.read_only_connection(c, timeout_s=0.4) as sa:
            sa.execute(runaway).fetchall()


def test_progress_handler_is_removed_after_use(target_sqlite):
    # A leaked handler would make the next query on the pooled connection
    # inherit a deadline that has already passed.
    c = sqlite_conn(target_sqlite)
    with reg.read_only_connection(c, timeout_s=0.4) as sa:
        sa.execute(text("SELECT 1")).fetchone()
    with reg.read_only_connection(c, timeout_s=5) as sa:
        assert sa.execute(text("SELECT count(*) FROM txns")).scalar() == 5


# ---------------------------------------------------------------------------
# Pooling
# ---------------------------------------------------------------------------


def test_engine_is_reused_for_same_connection_id(target_sqlite):
    c = sqlite_conn(target_sqlite)
    assert reg.get_engine(c) is reg.get_engine(c)


def test_dispose_engine_forces_a_new_one(target_sqlite):
    c = sqlite_conn(target_sqlite)
    first = reg.get_engine(c)
    reg.dispose_engine(c.id)
    assert reg.get_engine(c) is not first


def test_lru_evicts_beyond_cap(target_sqlite, monkeypatch):
    monkeypatch.setenv("FAE_MAX_TARGET_ENGINES", "3")
    get_settings.cache_clear()
    reg.dispose_all()

    conns = [sqlite_conn(target_sqlite, cid=f"c{i}") for i in range(5)]
    for c in conns:
        reg.get_engine(c)

    assert reg.registry_size() == 3
    # The three most recent survive; the two oldest were evicted.
    assert reg.get_engine(conns[4]) is not None


def test_touching_an_engine_makes_it_most_recent(target_sqlite, monkeypatch):
    monkeypatch.setenv("FAE_MAX_TARGET_ENGINES", "2")
    get_settings.cache_clear()
    reg.dispose_all()

    a = sqlite_conn(target_sqlite, cid="a")
    b = sqlite_conn(target_sqlite, cid="b")
    c = sqlite_conn(target_sqlite, cid="c")

    engine_a = reg.get_engine(a)
    reg.get_engine(b)
    reg.get_engine(a)  # refresh a
    reg.get_engine(c)  # should evict b, not a

    assert reg.registry_size() == 2
    assert reg.get_engine(a) is engine_a


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


class _FakePgError(Exception):
    def __init__(self, sqlstate: str, message: str = "pg failure"):
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeMySQLError(Exception):
    pass


def _wrap(orig: Exception) -> OperationalError:
    err = OperationalError("SELECT 1", {}, orig)
    return err


@pytest.mark.parametrize(
    "sqlstate,expected_code",
    [
        ("28P01", ErrorCode.DB_AUTH_FAILED),
        ("28000", ErrorCode.DB_AUTH_FAILED),
        ("42501", ErrorCode.DB_PERMISSION_DENIED),
        ("25006", ErrorCode.DB_PERMISSION_DENIED),
        ("57014", ErrorCode.QUERY_TIMEOUT),
        ("08006", ErrorCode.DB_UNREACHABLE),
        ("08001", ErrorCode.DB_UNREACHABLE),
        ("42P01", ErrorCode.QUERY_EXECUTION_ERROR),
    ],
)
def test_postgres_sqlstate_mapping(sqlstate, expected_code):
    err = reg.translate_db_error(_wrap(_FakePgError(sqlstate)))
    assert err.error_code == expected_code


@pytest.mark.parametrize(
    "errno,expected_code",
    [
        (1045, ErrorCode.DB_AUTH_FAILED),
        (1142, ErrorCode.DB_PERMISSION_DENIED),
        (1792, ErrorCode.DB_PERMISSION_DENIED),
        (3024, ErrorCode.QUERY_TIMEOUT),
        (2003, ErrorCode.DB_UNREACHABLE),
        (1049, ErrorCode.DB_UNREACHABLE),
    ],
)
def test_mysql_errno_mapping(errno, expected_code):
    orig = _FakeMySQLError(errno, "mysql failure")
    assert reg.translate_db_error(_wrap(orig)).error_code == expected_code


def test_sqlite_readonly_maps_to_permission_denied():
    orig = sqlite3.OperationalError("attempt to write a readonly database")
    assert reg.translate_db_error(_wrap(orig)).error_code == ErrorCode.DB_PERMISSION_DENIED


def test_sqlite_missing_file_maps_to_unreachable():
    orig = sqlite3.OperationalError("unable to open database file")
    assert reg.translate_db_error(_wrap(orig)).error_code == ErrorCode.DB_UNREACHABLE


def test_unknown_error_falls_back_to_query_execution_error():
    assert (
        reg.translate_db_error(_wrap(Exception("something odd"))).error_code
        == ErrorCode.QUERY_EXECUTION_ERROR
    )


def test_app_errors_pass_through_untranslated():
    original = DbAuthError("already classified")
    assert reg.translate_db_error(original) is original


def test_timed_out_flag_wins_over_message():
    assert (
        reg.translate_db_error(Exception("interrupted"), timed_out=True).error_code
        == ErrorCode.QUERY_TIMEOUT
    )


def test_error_messages_are_single_line_and_bounded():
    orig = Exception("line one\nline two\n" + "x" * 2000)
    message = reg.translate_db_error(_wrap(orig)).message
    assert "\n" not in message
    assert len(message) <= 500


def test_missing_table_is_a_400_not_a_500(target_sqlite):
    c = sqlite_conn(target_sqlite)
    with pytest.raises(QueryExecutionError) as ei:
        with reg.read_only_connection(c) as sa:
            sa.execute(text("SELECT * FROM no_such_table")).fetchall()
    assert ei.value.http_status == 400
