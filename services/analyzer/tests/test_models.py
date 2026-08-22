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
