import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    ChartType,
    Connection,
    ConnectionStatus,
    DbType,
    QueryExecutionLog,
    SavedQuery,
)


def _connection(**over) -> Connection:
    data = dict(name="c1", db_type=DbType.SQLITE, sqlite_path="/tmp/x.db")
    data.update(over)
    return Connection(**data)


def test_connection_defaults_to_untested(session):
    c = _connection()
    session.add(c)
    session.commit()
    assert c.status == ConnectionStatus.UNTESTED
    assert c.id and len(c.id) == 36
    assert c.created_at is not None


def test_saved_query_defaults(session):
    c = _connection()
    session.add(c)
    session.commit()
    q = SavedQuery(connection_id=c.id, name="q1", sql_text="SELECT 1")
    session.add(q)
    session.commit()
    assert q.row_limit == 1000
    assert q.chart_type == ChartType.TABLE


def test_duplicate_query_name_per_connection_rejected(session):
    c = _connection()
    session.add(c)
    session.commit()
    session.add(SavedQuery(connection_id=c.id, name="dupe", sql_text="SELECT 1"))
    session.commit()
    session.add(SavedQuery(connection_id=c.id, name="dupe", sql_text="SELECT 2"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_query_name_allowed_on_different_connections(session):
    a, b = _connection(name="a"), _connection(name="b")
    session.add_all([a, b])
    session.commit()
    session.add_all(
        [
            SavedQuery(connection_id=a.id, name="same", sql_text="SELECT 1"),
            SavedQuery(connection_id=b.id, name="same", sql_text="SELECT 1"),
        ]
    )
    session.commit()
    assert session.query(SavedQuery).count() == 2


def test_duplicate_connection_name_rejected(session):
    session.add(_connection(name="dupe"))
    session.commit()
    session.add(_connection(name="dupe"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_connection_cascades_to_queries_and_logs(session):
    c = _connection()
    session.add(c)
    session.commit()
    q = SavedQuery(connection_id=c.id, name="q1", sql_text="SELECT 1")
    session.add(q)
    session.commit()
    session.add(QueryExecutionLog(query_id=q.id, row_count=1, duration_ms=2))
    session.commit()

    assert session.query(SavedQuery).count() == 1
    assert session.query(QueryExecutionLog).count() == 1

    session.delete(c)
    session.commit()

    assert session.query(Connection).count() == 0
    assert session.query(SavedQuery).count() == 0
    assert session.query(QueryExecutionLog).count() == 0


def test_execution_log_records_failure(session):
    c = _connection()
    session.add(c)
    session.commit()
    q = SavedQuery(connection_id=c.id, name="q1", sql_text="SELECT 1")
    session.add(q)
    session.commit()
    log = QueryExecutionLog(
        query_id=q.id, success=False, error_code="QUERY_TIMEOUT", error_message="slow"
    )
    session.add(log)
    session.commit()
    assert log.success is False
    assert log.executed_at is not None


# ---------------------------------------------------------------------------
# Enum storage
# ---------------------------------------------------------------------------


def test_enum_columns_store_values_not_names(session):
    """Regression: SQLAlchemy stores enum *names* by default.

    That made every server_default (written as a value) unreadable by the ORM,
    and made a client reading the database directly see different strings than
    the API returns for the same field.
    """
    from sqlalchemy import text

    c = _connection()
    session.add(c)
    session.commit()
    session.add(
        SavedQuery(
            connection_id=c.id, name="q", sql_text="SELECT 1", chart_type=ChartType.LINE
        )
    )
    session.commit()

    stored_db_type = session.execute(text("SELECT db_type FROM connections")).scalar()
    stored_status = session.execute(text("SELECT status FROM connections")).scalar()
    stored_chart = session.execute(text("SELECT chart_type FROM saved_queries")).scalar()

    assert stored_db_type == "sqlite"
    assert stored_status == "untested"
    assert stored_chart == "line"


def test_a_row_written_without_the_orm_is_readable_by_it(session):
    """A migration, seed script, or direct client write must round-trip."""
    from sqlalchemy import text

    session.execute(
        text(
            "INSERT INTO connections (id, name, db_type, sqlite_path,"
            " created_at, updated_at)"
            " VALUES ('raw-1', 'raw', 'sqlite', '/tmp/x.db',"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    session.commit()
    session.expire_all()

    conn = session.get(Connection, "raw-1")
    assert conn.db_type == DbType.SQLITE
    assert conn.status == ConnectionStatus.UNTESTED  # from server_default


def test_server_defaults_match_the_stored_spelling():
    from app.models import ChartType, ConnectionStatus

    assert Connection.__table__.c.status.server_default.arg == ConnectionStatus.UNTESTED.value
    assert Connection.__table__.c.status.type.enums == [e.value for e in ConnectionStatus]
    assert SavedQuery.__table__.c.chart_type.type.enums == [e.value for e in ChartType]
