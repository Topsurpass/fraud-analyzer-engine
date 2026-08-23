"""Execution-log retention.

One card polling at the default interval writes roughly 17k rows a day on
cache misses, forever. Nothing in the service ever deleted them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import get_settings
from app.models import QueryExecutionLog, utcnow
from app.services import saved_query_service as svc


@pytest.fixture
def query_id(client, sqlite_connection):
    created = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={"name": "q", "sql_text": "SELECT day FROM txns", "chart_type": "table"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _add_logs(session, query_id, count, age_days=0):
    stamp = utcnow() - timedelta(days=age_days)
    for i in range(count):
        session.add(
            QueryExecutionLog(
                query_id=query_id,
                success=True,
                row_count=1,
                duration_ms=1,
                executed_at=stamp - timedelta(seconds=i),
            )
        )
    session.commit()


def test_rows_past_the_retention_window_are_deleted(session, query_id, monkeypatch):
    monkeypatch.setenv("FAE_LOG_RETENTION_DAYS", "30")
    monkeypatch.setenv("FAE_MAX_LOGS_PER_QUERY", "0")
    get_settings.cache_clear()

    _add_logs(session, query_id, 5, age_days=60)
    _add_logs(session, query_id, 3, age_days=1)

    removed = svc.prune_execution_logs(session)
    assert removed == 5
    assert len(svc.recent_logs(session, query_id, limit=100)) == 3


def test_rows_inside_the_window_are_kept(session, query_id, monkeypatch):
    monkeypatch.setenv("FAE_LOG_RETENTION_DAYS", "30")
    monkeypatch.setenv("FAE_MAX_LOGS_PER_QUERY", "0")
    get_settings.cache_clear()

    _add_logs(session, query_id, 4, age_days=2)
    assert svc.prune_execution_logs(session) == 0
    assert len(svc.recent_logs(session, query_id, limit=100)) == 4


def test_per_query_depth_is_capped(session, query_id, monkeypatch):
    """Age alone lets one busy card bury every other query inside the window."""
    monkeypatch.setenv("FAE_LOG_RETENTION_DAYS", "0")
    monkeypatch.setenv("FAE_MAX_LOGS_PER_QUERY", "10")
    get_settings.cache_clear()

    _add_logs(session, query_id, 25)
    svc.prune_execution_logs(session)

    remaining = svc.recent_logs(session, query_id, limit=100)
    assert len(remaining) <= 10


def test_the_newest_rows_are_the_ones_kept(session, query_id, monkeypatch):
    monkeypatch.setenv("FAE_LOG_RETENTION_DAYS", "0")
    monkeypatch.setenv("FAE_MAX_LOGS_PER_QUERY", "5")
    get_settings.cache_clear()

    _add_logs(session, query_id, 20)
    before = svc.recent_logs(session, query_id, limit=5)
    svc.prune_execution_logs(session)
    after = svc.recent_logs(session, query_id, limit=100)

    assert [entry.id for entry in after] == [entry.id for entry in before]


def test_pruning_is_disabled_when_both_limits_are_zero(session, query_id, monkeypatch):
    monkeypatch.setenv("FAE_LOG_RETENTION_DAYS", "0")
    monkeypatch.setenv("FAE_MAX_LOGS_PER_QUERY", "0")
    get_settings.cache_clear()

    _add_logs(session, query_id, 5, age_days=900)
    assert svc.prune_execution_logs(session) == 0
    assert len(svc.recent_logs(session, query_id, limit=100)) == 5


def test_a_query_under_quota_is_untouched(session, query_id, monkeypatch):
    monkeypatch.setenv("FAE_LOG_RETENTION_DAYS", "0")
    monkeypatch.setenv("FAE_MAX_LOGS_PER_QUERY", "50")
    get_settings.cache_clear()

    _add_logs(session, query_id, 3)
    assert svc.prune_execution_logs(session) == 0
    assert len(svc.recent_logs(session, query_id, limit=100)) == 3


def test_startup_pruning_never_blocks_the_service(client, monkeypatch):
    """An unprunable log table is housekeeping, not a reason to refuse to serve."""
    import app.main as main

    def explode(*_args, **_kwargs):
        raise RuntimeError("prune failed")

    monkeypatch.setattr(svc, "prune_execution_logs", explode)
    main._prune_logs_on_startup()  # must not raise
    assert client.get("/health").status_code == 200
