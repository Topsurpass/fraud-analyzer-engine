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
from app.models.enums import SslMode
from app.security.crypto import encrypt


def _pg(db_type: DbType = DbType.POSTGRES, **over) -> Connection:
    """A network connection with everything the TLS mapping reads."""
    fields = {
        "name": "t",
        "db_type": db_type,
        "host": "db.example.test",
        "database": "d",
        "username": "u",
        "ssl_mode": SslMode.REQUIRE,
        "ssl_root_cert": None,
    }
    fields.update(over)
    return Connection(**fields)


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


def test_mysql_socket_timeout_is_padded_beyond_the_statement_timeout(monkeypatch):
    """Regression: the socket must not time out before the server does.

    When read_timeout equalled max_execution_time the socket usually won the
    race, and a merely slow query surfaced as errno 2013 "lost connection" ->
    502 DB_UNREACHABLE instead of 504 QUERY_TIMEOUT. Covered here without a
    MySQL server; tests/test_live_targets.py proves the resulting error code
    against a real one.
    """
    monkeypatch.setenv("FAE_QUERY_TIMEOUT_S", "10")
    get_settings.cache_clear()

    args = reg.mysql_connect_args(_pg(DbType.MYSQL))
    assert args["read_timeout"] == 15
    assert args["read_timeout"] > get_settings().query_timeout_s
    assert args["write_timeout"] > get_settings().query_timeout_s


def test_postgres_connect_args_pin_read_only_and_timeout(monkeypatch):
    monkeypatch.setenv("FAE_QUERY_TIMEOUT_S", "7")
    get_settings.cache_clear()

    options = reg.postgres_connect_args(_pg())["options"]
    assert "default_transaction_read_only=on" in options
    assert "statement_timeout=7000" in options
    assert "idle_in_transaction_session_timeout=7000" in options


def test_sqlite_interrupt_message_maps_to_timeout():
    orig = sqlite3.OperationalError("interrupted")
    assert reg.translate_db_error(_wrap(orig)).error_code == ErrorCode.QUERY_TIMEOUT


@pytest.mark.parametrize(
    "message,expected",
    [
        ("connection timed out", ErrorCode.QUERY_TIMEOUT),
        ("statement timeout exceeded", ErrorCode.QUERY_TIMEOUT),
        ("authentication failed for user", ErrorCode.DB_AUTH_FAILED),
        ("Access denied for user 'x'", ErrorCode.DB_AUTH_FAILED),
        ("could not connect to server", ErrorCode.DB_UNREACHABLE),
        ("connection refused", ErrorCode.DB_UNREACHABLE),
    ],
)
def test_message_fallbacks_for_drivers_without_codes(message, expected):
    # Last resort for a driver that supplies neither SQLSTATE nor errno.
    assert reg.translate_db_error(_wrap(Exception(message))).error_code == expected


# --------------------------------------------------------------------------
# Pool exhaustion
#
# Regression: no pool_timeout was set, so SQLAlchemy's 30 s default applied.
# The resulting sqlalchemy.exc.TimeoutError fell through translate_db_error to
# the text match on "timed out" and surfaced as QUERY_TIMEOUT (504), telling
# the frontend a query was slow when in fact the service had no free
# connection and the query never ran. The driver message also spelled out the
# pool configuration to an unauthenticated caller.
# --------------------------------------------------------------------------


def test_pool_exhaustion_is_not_reported_as_a_query_timeout():
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from app.db.target_registry import translate_db_error
    from app.errors import ErrorCode

    exhausted = PoolTimeout(
        "QueuePool limit of size 5 overflow 2 reached, connection timed out, "
        "timeout 30.00"
    )
    translated = translate_db_error(exhausted)

    assert translated.error_code == ErrorCode.DB_UNREACHABLE
    assert translated.error_code != ErrorCode.QUERY_TIMEOUT


def test_pool_exhaustion_does_not_leak_the_pool_configuration():
    """The raw driver message names the pool size and overflow."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from app.db.target_registry import translate_db_error

    translated = translate_db_error(
        PoolTimeout("QueuePool limit of size 5 overflow 2 reached, timeout 30.00")
    )

    assert "QueuePool" not in translated.message
    assert "overflow" not in translated.message
    assert translated.detail == {"reason": "pool_exhausted"}


def test_pool_timeout_is_configured_on_target_engines(session, target_sqlite):
    """Without this the default is 30 s, past the frontend's poll deadline."""
    from app.config import get_settings
    from app.db import target_registry
    from app.models import Connection, DbType

    conn = Connection(name="p", db_type=DbType.SQLITE, sqlite_path=target_sqlite)
    session.add(conn)
    session.commit()

    engine = target_registry.get_engine(conn)
    assert engine.pool.timeout() == get_settings().target_pool_timeout_s


# ---------------------------------------------------------------------------
# TLS mode -> driver arguments
#
# The whole point of the SslMode column is that these two dicts say what they
# mean instead of letting each driver pick a default. libpq's own default is
# "prefer", which offers plaintext first and accepts it silently: it makes a
# target that requires TLS unreachable and downgrades one that merely tolerates
# plaintext. Both mappings are pure, so the matrix needs no database.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(SslMode))
def test_postgres_sends_every_mode_verbatim(mode):
    # libpq speaks these spellings natively, so the enum *is* the mapping. A
    # translation table here would be a place for the two to drift apart.
    args = reg.postgres_connect_args(_pg(ssl_mode=mode))
    assert args["sslmode"] == mode.value


def test_postgres_never_omits_sslmode():
    # The bug this column exists for: no sslmode at all means libpq's "prefer".
    assert "sslmode" in reg.postgres_connect_args(_pg())


@pytest.mark.parametrize("mode", [SslMode.VERIFY_CA, SslMode.VERIFY_FULL])
def test_verifying_modes_carry_the_connection_certificate(mode):
    args = reg.postgres_connect_args(_pg(ssl_mode=mode, ssl_root_cert="/ca/mine.crt"))
    assert args["sslrootcert"] == "/ca/mine.crt"


@pytest.mark.parametrize(
    "mode", [SslMode.DISABLE, SslMode.ALLOW, SslMode.PREFER, SslMode.REQUIRE]
)
def test_non_verifying_modes_carry_no_certificate(mode):
    # Sending a root cert under a mode that never checks it would read as a
    # protection that is not happening.
    assert "sslrootcert" not in reg.postgres_connect_args(_pg(ssl_mode=mode))


def test_verify_full_falls_back_to_the_system_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "ca-certificates.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(reg, "_CA_BUNDLE_CANDIDATES", (str(bundle),))
    get_settings.cache_clear()

    args = reg.postgres_connect_args(_pg(ssl_mode=SslMode.VERIFY_FULL))
    assert args["sslrootcert"] == str(bundle)


def test_the_setting_outranks_the_system_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "system.crt"
    bundle.write_text("x")
    monkeypatch.setattr(reg, "_CA_BUNDLE_CANDIDATES", (str(bundle),))
    monkeypatch.setenv("FAE_TARGET_SSL_ROOT_CERT", "/etc/internal-ca.crt")
    get_settings.cache_clear()

    args = reg.postgres_connect_args(_pg(ssl_mode=SslMode.VERIFY_FULL))
    assert args["sslrootcert"] == "/etc/internal-ca.crt"


def test_the_connection_outranks_the_setting(monkeypatch):
    monkeypatch.setenv("FAE_TARGET_SSL_ROOT_CERT", "/etc/internal-ca.crt")
    get_settings.cache_clear()

    args = reg.postgres_connect_args(
        _pg(ssl_mode=SslMode.VERIFY_FULL, ssl_root_cert="/ca/mine.crt")
    )
    assert args["sslrootcert"] == "/ca/mine.crt"


def test_verify_full_without_any_bundle_says_nothing(monkeypatch):
    # Better than inventing a path: libpq then fails naming the file it wanted.
    monkeypatch.setattr(reg, "_CA_BUNDLE_CANDIDATES", ())
    get_settings.cache_clear()
    assert "sslrootcert" not in reg.postgres_connect_args(_pg(ssl_mode=SslMode.VERIFY_FULL))


@pytest.mark.parametrize("mode", [SslMode.DISABLE, SslMode.ALLOW, SslMode.PREFER])
def test_mysql_leaves_tls_to_the_server_below_require(mode):
    # pymysql insists on TLS the moment any ssl argument is passed, so there is
    # no way to express "try, then fall back". Sending nothing is the honest
    # translation, and is what this driver did before the mode existed.
    args = reg.mysql_connect_args(_pg(DbType.MYSQL, ssl_mode=mode))
    assert "ssl" not in args


def test_mysql_require_encrypts_without_checking_the_certificate():
    args = reg.mysql_connect_args(_pg(DbType.MYSQL, ssl_mode=SslMode.REQUIRE))
    assert args["ssl"] == {}
    assert args["ssl_verify_cert"] is False
    assert args["ssl_verify_identity"] is False


def test_mysql_verify_ca_checks_the_certificate_but_not_the_hostname():
    args = reg.mysql_connect_args(
        _pg(DbType.MYSQL, ssl_mode=SslMode.VERIFY_CA, ssl_root_cert="/ca/mine.crt")
    )
    assert args["ssl"] == {"ca": "/ca/mine.crt"}
    assert args["ssl_verify_cert"] is True
    assert args["ssl_verify_identity"] is False


def test_mysql_verify_full_also_checks_the_hostname():
    args = reg.mysql_connect_args(
        _pg(DbType.MYSQL, ssl_mode=SslMode.VERIFY_FULL, ssl_root_cert="/ca/mine.crt")
    )
    assert args["ssl_verify_identity"] is True


def test_tls_settings_never_disturb_the_read_only_pinning():
    # The read-only options are the first line of defence and share this dict.
    args = reg.postgres_connect_args(_pg(ssl_mode=SslMode.VERIFY_FULL))
    assert "default_transaction_read_only=on" in args["options"]


# ---------------------------------------------------------------------------
# The multi-attempt message
#
# Verbatim from a real Neon connection inside the analyzer container. Managed
# Postgres resolves to several addresses; psycopg tries each and reports the
# LAST failure as the headline, then lists them all. The host has an AAAA
# record and the container has no IPv6 route, so the headline is always the
# IPv6 "Network is unreachable" - while the IPv4 attempts, the ones that
# actually reached the server, carry the real reason further down.
#
# Truncating this from the front kept the noise and dropped the answer, which
# is how a one-line TLS misconfiguration presented as a network outage.
# ---------------------------------------------------------------------------

_NEON_TLS_REFUSAL = """connection is bad: connection to server at "2600:1f18:6fa0:b306:5f78:3188:6ba5:10d7", port 5432 failed: Network is unreachable
\tIs the server running on that host and accepting TCP/IP connections?
Multiple connection attempts failed. All failures were:
- host: 'ep-example.aws.neon.tech', port: 5432, hostaddr: '23.21.74.185': connection failed: connection to server at "23.21.74.185", port 5432 failed: ERROR:  connection is insecure (try using `sslmode=require`)
- host: 'ep-example.aws.neon.tech', port: 5432, hostaddr: '98.89.62.209': connection failed: connection to server at "98.89.62.209", port 5432 failed: ERROR:  connection is insecure (try using `sslmode=require`)
- host: 'ep-example.aws.neon.tech', port: 5432, hostaddr: '2600:1f18:6fa0:b306:5f78:3188:6ba5:10d7': connection is bad: connection to server at "2600:1f18:6fa0:b306:5f78:3188:6ba5:10d7", port 5432 failed: Network is unreachable
\tIs the server running on that host and accepting TCP/IP connections?"""


def test_a_tls_refusal_is_not_reported_as_a_network_outage():
    error = reg.translate_db_error(_wrap(OperationalError(_NEON_TLS_REFUSAL, {}, None)))
    assert error.error_code == ErrorCode.DB_TLS_REQUIRED
    # The message must name the field to change, not the symptom.
    assert "TLS mode" in error.message
    assert "Network is unreachable" not in error.message


def test_the_cleaned_message_keeps_the_attempt_that_reached_the_server():
    cleaned = reg._clean(OperationalError(_NEON_TLS_REFUSAL, {}, None))
    assert "connection is insecure" in cleaned
    assert len(cleaned) <= 500


def test_identical_attempts_are_not_repeated_three_times():
    # Three addresses of one host usually fail identically. Three copies of the
    # same sentence crowd out the one attempt that differs.
    cleaned = reg._clean(OperationalError(_NEON_TLS_REFUSAL, {}, None))
    assert cleaned.count("connection is insecure") == 1


def test_a_genuine_ipv6_only_failure_still_reports_unreachable():
    # The fix must not swallow a real routing problem: when every attempt says
    # the network is unreachable, that IS the answer.
    message = """connection is bad: connection to server at "2600::1", port 5432 failed: Network is unreachable
Multiple connection attempts failed. All failures were:
- host: 'x.example', port: 5432, hostaddr: '2600::1': connection is bad: connection to server at "2600::1", port 5432 failed: Network is unreachable"""
    error = reg.translate_db_error(_wrap(OperationalError(message, {}, None)))
    assert error.error_code == ErrorCode.DB_UNREACHABLE


def test_a_single_attempt_message_is_untouched():
    orig = OperationalError('connection to server at "10.0.0.1" failed: Connection refused', {}, None)
    assert "Connection refused" in reg._clean(orig)
