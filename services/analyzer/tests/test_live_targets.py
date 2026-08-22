"""Live integration against real PostgreSQL and MySQL servers.

Skipped automatically when a server is not reachable, so the default suite
stays hermetic. Bring them up with:

    docker run -d --name fae-pg -e POSTGRES_PASSWORD=rootpw -e POSTGRES_DB=fraud \\
        -p 55432:5432 postgres:16-alpine
    docker run -d --name fae-my -e MYSQL_ROOT_PASSWORD=rootpw -e MYSQL_DATABASE=fraud \\
        -p 53306:3306 mysql:8

then seed them with the DDL in ``tests/fixtures/live_seed.sql``.

These tests matter because SQLite cannot exercise the code paths that protect a
production database: ``default_transaction_read_only``, ``statement_timeout``,
``SET SESSION TRANSACTION READ ONLY``, ``max_execution_time``, SQLSTATE-based
error mapping, and the driver-specific exfiltration functions.

The application user in these fixtures is deliberately granted FULL WRITE
ACCESS. That is the point: it means the only thing preventing a write is the
service's own enforcement, not the database role. A read-only role is what the
README tells operators to use in production, but a test that relied on one
would prove nothing about this code.
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import target_registry as reg
from app.errors import (
    DbAuthError,
    DbPermissionError,
    DbUnreachableError,
    QueryExecutionError,
    QueryTimeoutError,
    SqlValidationError,
)
from app.models import Connection, DbType
from app.security.crypto import encrypt
from app.services.query_service import execute_sql

PG = {"host": "127.0.0.1", "port": 55432, "database": "fraud", "username": "app_rw", "password": "apppw"}
MY = {"host": "127.0.0.1", "port": 53306, "database": "fraud", "username": "app_rw", "password": "apppw"}


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _conn(db_type: DbType, cfg: dict, cid: str) -> Connection:
    c = Connection(
        name=cid,
        db_type=db_type,
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        username=cfg["username"],
        password_encrypted=encrypt(cfg["password"]),
    )
    c.id = cid
    return c


postgres_only = pytest.mark.skipif(
    not _reachable(PG["host"], PG["port"]), reason="no PostgreSQL on 127.0.0.1:55432"
)
mysql_only = pytest.mark.skipif(
    not _reachable(MY["host"], MY["port"]), reason="no MySQL on 127.0.0.1:53306"
)


@pytest.fixture
def pg():
    return _conn(DbType.POSTGRES, PG, "live-pg")


@pytest.fixture
def my():
    return _conn(DbType.MYSQL, MY, "live-my")


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


@postgres_only
def test_pg_reads(pg):
    result = execute_sql(pg, "SELECT count(*) AS n FROM payments", row_limit=10)
    assert result.rows == [[4]]


@postgres_only
def test_pg_probe(pg):
    reg.probe(pg)


@postgres_only
def test_pg_session_is_read_only_even_with_a_write_privileged_role(pg):
    # The role has full write grants. Only default_transaction_read_only=on
    # stops this, which is exactly what is under test.
    with pytest.raises(DbPermissionError) as ei:
        with reg.read_only_connection(pg) as sa:
            sa.execute(text("INSERT INTO payments (merchant) VALUES ('evil')"))
    assert ei.value.http_status == 403


@postgres_only
def test_pg_cannot_create_a_table(pg):
    with pytest.raises(DbPermissionError):
        with reg.read_only_connection(pg) as sa:
            sa.execute(text("CREATE TABLE evil (a int)"))


@postgres_only
def test_pg_cannot_delete(pg):
    with pytest.raises(DbPermissionError):
        with reg.read_only_connection(pg) as sa:
            sa.execute(text("DELETE FROM payments"))
    assert execute_sql(pg, "SELECT count(*) FROM payments", 10).rows == [[4]]


@postgres_only
def test_pg_session_variables_are_actually_set(pg):
    with reg.read_only_connection(pg) as sa:
        assert sa.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
        assert sa.execute(text("SHOW statement_timeout")).scalar() == (
            f"{get_settings().query_timeout_s}s"
        )


@postgres_only
def test_pg_statement_timeout_fires(pg):
    # pg_sleep is blocked by the guard, so drive the timeout with real work.
    with pytest.raises(QueryTimeoutError) as ei:
        with reg.read_only_connection(pg) as sa:
            sa.execute(
                text("SELECT count(*) FROM generate_series(1, 2000000000)")
            ).fetchall()
    assert ei.value.http_status == 504


@postgres_only
def test_pg_bad_credentials_map_to_401(pg):
    bad = _conn(DbType.POSTGRES, {**PG, "password": "wrong"}, "live-pg-bad")
    with pytest.raises(DbAuthError) as ei:
        reg.probe(bad)
    assert ei.value.http_status == 401


@postgres_only
def test_pg_unknown_table_is_a_400(pg):
    with pytest.raises(QueryExecutionError) as ei:
        execute_sql(pg, "SELECT * FROM no_such_table", 10)
    assert ei.value.http_status == 400


@postgres_only
def test_pg_unreachable_port_maps_to_502():
    dead = _conn(DbType.POSTGRES, {**PG, "port": 1}, "live-pg-dead")
    with pytest.raises(DbUnreachableError) as ei:
        reg.probe(dead)
    assert ei.value.http_status == 502


@postgres_only
def test_pg_numeric_and_date_types_serialise(pg):
    result = execute_sql(
        pg, "SELECT day, amount, chargeback, created_at FROM payments ORDER BY id", 10
    )
    day, amount, chargeback, created = result.rows[0]
    assert day == "2026-08-19"
    assert amount == "120.00"  # Decimal keeps its exact text form
    assert chargeback is True
    assert created.startswith("2026-")


@postgres_only
def test_pg_row_cap_and_truncation(pg):
    result = execute_sql(pg, "SELECT * FROM payments ORDER BY id", row_limit=2)
    assert result.row_count == 2
    assert result.truncated is True


@postgres_only
def test_pg_exfil_function_blocked_before_reaching_the_server(pg):
    with pytest.raises(SqlValidationError):
        execute_sql(pg, "SELECT pg_read_file('/etc/passwd')", 10)


@postgres_only
def test_pg_cte_write_blocked(pg):
    with pytest.raises(SqlValidationError):
        execute_sql(
            pg,
            "WITH x AS (INSERT INTO payments (merchant) VALUES ('e') RETURNING *)"
            " SELECT * FROM x",
            10,
        )
    assert execute_sql(pg, "SELECT count(*) FROM payments", 10).rows == [[4]]


@postgres_only
def test_pg_introspection(pg):
    from app.services.introspection_service import list_columns, list_tables

    names = {t.name: t.kind for t in list_tables(pg)}
    assert names["payments"] == "table"
    assert names["flagged"] == "view"
    columns = {c.name for c in list_columns(pg, "payments")}
    assert {"id", "day", "merchant", "amount", "chargeback"} <= columns


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------


@mysql_only
def test_mysql_reads(my):
    assert execute_sql(my, "SELECT count(*) AS n FROM payments", 10).rows == [[4]]


@mysql_only
def test_mysql_probe(my):
    reg.probe(my)


@mysql_only
def test_mysql_session_is_read_only_even_with_a_write_privileged_role(my):
    with pytest.raises(DbPermissionError):
        with reg.read_only_connection(my) as sa:
            sa.execute(text("INSERT INTO payments (merchant) VALUES ('evil')"))


@mysql_only
def test_mysql_cannot_delete(my):
    with pytest.raises(DbPermissionError):
        with reg.read_only_connection(my) as sa:
            sa.execute(text("DELETE FROM payments"))
    assert execute_sql(my, "SELECT count(*) FROM payments", 10).rows == [[4]]


@mysql_only
def test_mysql_bad_credentials_map_to_401(my):
    bad = _conn(DbType.MYSQL, {**MY, "password": "wrong"}, "live-my-bad")
    with pytest.raises(DbAuthError) as ei:
        reg.probe(bad)
    assert ei.value.http_status == 401


@mysql_only
def test_mysql_unknown_table_is_a_400(my):
    with pytest.raises(QueryExecutionError):
        execute_sql(my, "SELECT * FROM no_such_table", 10)


@mysql_only
def test_mysql_unreachable_port_maps_to_502():
    dead = _conn(DbType.MYSQL, {**MY, "port": 1}, "live-my-dead")
    with pytest.raises(DbUnreachableError):
        reg.probe(dead)


@mysql_only
def test_mysql_session_variables_are_actually_set(my):
    # Stronger and faster than trying to trip a timeout: assert the server
    # reports the settings the connect hook is supposed to have applied.
    with reg.read_only_connection(my) as sa:
        assert sa.execute(text("SELECT @@SESSION.transaction_read_only")).scalar() == 1
        assert sa.execute(text("SELECT @@SESSION.max_execution_time")).scalar() == (
            get_settings().query_timeout_ms
        )


@mysql_only
def test_mysql_max_execution_time_fires(my, monkeypatch):
    monkeypatch.setenv("FAE_QUERY_TIMEOUT_S", "2")
    get_settings.cache_clear()
    reg.dispose_engine(my.id)

    # A plain cross join is counted without being materialised, so it finishes
    # in milliseconds. Forcing a per-row md5 makes the work real.
    heavy = text(
        "SELECT count(*) FROM payments a, payments b, payments c, payments d,"
        " payments e, payments f, payments g, payments h, payments i,"
        " payments j, payments k, payments l"
        " WHERE md5(concat(a.id,b.id,c.id,d.id,e.id,f.id,"
        "g.id,h.id,i.id,j.id,k.id,l.id)) > ''"
    )
    with pytest.raises(QueryTimeoutError) as ei:
        with reg.read_only_connection(my) as sa:
            sa.execute(heavy).fetchall()
    assert ei.value.http_status == 504


@mysql_only
def test_mysql_row_cap_with_duplicate_column_names(my):
    # The subquery wrapper this service deliberately avoids would fail here
    # with "Duplicate column name 'a'".
    result = execute_sql(my, "SELECT id AS a, merchant AS a FROM payments", row_limit=2)
    assert result.row_count == 2


@mysql_only
def test_mysql_introspection(my):
    from app.services.introspection_service import list_tables

    assert "payments" in {t.name for t in list_tables(my)}
