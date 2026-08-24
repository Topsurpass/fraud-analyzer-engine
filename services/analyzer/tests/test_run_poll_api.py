"""Execution endpoints: /run, /poll, /preview, and execution logging."""

from __future__ import annotations

import sqlite3

import pytest

from app.models import QueryExecutionLog
from app.services import result_cache

RUN_KEYS = {
    "query_id",
    "executed_at",
    "duration_ms",
    "row_count",
    "truncated",
    "data_hash",
    "columns",
    "rows",
    "chart",
    # Always present, empty when the query defines no flag rules, so the
    # frontend never has to branch on the key existing.
    "flags",
    "poll_interval_ms",
}


@pytest.fixture(autouse=True)
def _clear_cache():
    result_cache.clear()
    yield
    result_cache.clear()


@pytest.fixture
def saved(client, sqlite_connection):
    r = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={
            "name": "flagged per day",
            "sql_text": (
                "SELECT day, count(*) AS flagged_count FROM txns "
                "WHERE flagged = 1 GROUP BY day ORDER BY day"
            ),
            "chart_type": "line",
            "x_field": "day",
            "y_field": "flagged_count",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _insert_row(path: str, day: str = "2026-08-22") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO txns (day, user_id, amount, flagged) VALUES (?, 9, 5.0, 1)", (day,)
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# /run
# ---------------------------------------------------------------------------


def test_run_returns_the_documented_payload(client, saved):
    r = client.post(f"/queries/{saved['id']}/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == RUN_KEYS
    assert body["columns"] == ["day", "flagged_count"]
    assert body["rows"] == [["2026-08-19", 1], ["2026-08-20", 1]]
    assert body["row_count"] == 2
    assert body["truncated"] is False
    assert body["data_hash"].startswith("sha256:")
    assert body["chart"] == {
        "type": "line",
        "x_field": "day",
        "y_field": "flagged_count",
        "series_field": None,
        "warnings": [],
    }
    assert body["poll_interval_ms"] == 5000


def test_run_twice_gives_the_same_hash(client, saved):
    first = client.post(f"/queries/{saved['id']}/run").json()
    second = client.post(f"/queries/{saved['id']}/run").json()
    assert first["data_hash"] == second["data_hash"]


def test_run_reflects_new_data(client, saved, target_sqlite):
    before = client.post(f"/queries/{saved['id']}/run").json()
    _insert_row(target_sqlite)
    after = client.post(f"/queries/{saved['id']}/run").json()
    assert after["data_hash"] != before["data_hash"]
    assert after["row_count"] == before["row_count"] + 1


def test_run_on_missing_query_is_404(client):
    assert client.post("/queries/nope/run").status_code == 404


def test_run_writes_a_success_log(client, saved, session):
    client.post(f"/queries/{saved['id']}/run")
    logs = session.query(QueryExecutionLog).all()
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].row_count == 2
    assert logs[0].duration_ms is not None


def test_failed_run_writes_a_failure_log(client, saved, session, target_sqlite):
    # Drop the table out from under a saved query to force a real failure.
    writable = sqlite3.connect(target_sqlite)
    writable.execute("DROP TABLE txns")
    writable.commit()
    writable.close()

    r = client.post(f"/queries/{saved['id']}/run")
    assert r.status_code == 400
    assert r.json()["error_code"] == "QUERY_EXECUTION_ERROR"

    log = session.query(QueryExecutionLog).one()
    assert log.success is False
    assert log.error_code == "QUERY_EXECUTION_ERROR"
    assert log.error_message


def test_logs_endpoint_returns_newest_first(client, saved):
    client.post(f"/queries/{saved['id']}/run")
    client.post(f"/queries/{saved['id']}/run")
    r = client.get(f"/queries/{saved['id']}/logs")
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert all(entry["success"] for entry in r.json())


def test_truncation_is_reported(client, sqlite_connection):
    created = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={"name": "capped", "sql_text": "SELECT * FROM txns", "row_limit": 2},
    ).json()
    body = client.post(f"/queries/{created['id']}/run").json()
    assert body["row_count"] == 2
    assert body["truncated"] is True


def test_chart_warning_surfaces_without_failing(client, sqlite_connection):
    created = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={
            "name": "bad mapping",
            "sql_text": "SELECT day FROM txns",
            "chart_type": "line",
            "x_field": "day",
            "y_field": "not_a_column",
        },
    ).json()
    body = client.post(f"/queries/{created['id']}/run").json()
    assert body["row_count"] == 5
    assert any("not_a_column" in w for w in body["chart"]["warnings"])


# ---------------------------------------------------------------------------
# /poll
# ---------------------------------------------------------------------------


def test_poll_with_matching_hash_reports_unchanged(client, saved):
    run = client.post(f"/queries/{saved['id']}/run").json()
    r = client.get(f"/queries/{saved['id']}/poll", params={"since_hash": run["data_hash"]})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["data_hash"] == run["data_hash"]
    assert body["poll_interval_ms"] == 5000
    assert "rows" not in body


def test_unchanged_poll_does_not_touch_the_target_database(client, saved, session):
    run = client.post(f"/queries/{saved['id']}/run").json()
    before = session.query(QueryExecutionLog).count()

    for _ in range(5):
        r = client.get(
            f"/queries/{saved['id']}/poll", params={"since_hash": run["data_hash"]}
        )
        assert r.json()["changed"] is False
        assert r.json()["from_cache"] is True

    # No new execution logs means no query actually ran.
    assert session.query(QueryExecutionLog).count() == before


def test_poll_with_stale_hash_returns_the_full_payload(client, saved):
    client.post(f"/queries/{saved['id']}/run")
    r = client.get(f"/queries/{saved['id']}/poll", params={"since_hash": "sha256:stale"})
    body = r.json()
    assert body["changed"] is True
    assert body["rows"] == [["2026-08-19", 1], ["2026-08-20", 1]]
    assert set(body) == RUN_KEYS | {"changed", "from_cache"}


def test_poll_without_a_hash_returns_the_full_payload(client, saved):
    r = client.get(f"/queries/{saved['id']}/poll")
    assert r.json()["changed"] is True
    assert r.json()["columns"] == ["day", "flagged_count"]


def test_poll_on_a_cold_cache_executes(client, saved, session):
    r = client.get(f"/queries/{saved['id']}/poll")
    assert r.json()["from_cache"] is False
    assert session.query(QueryExecutionLog).count() == 1


def test_poll_detects_change_after_cache_expiry(client, saved, target_sqlite):
    run = client.post(f"/queries/{saved['id']}/run").json()
    _insert_row(target_sqlite)

    # While the cache is warm the old hash still matches, by design.
    warm = client.get(
        f"/queries/{saved['id']}/poll", params={"since_hash": run["data_hash"]}
    ).json()
    assert warm["changed"] is False

    result_cache.clear()
    cold = client.get(
        f"/queries/{saved['id']}/poll", params={"since_hash": run["data_hash"]}
    ).json()
    assert cold["changed"] is True
    assert cold["row_count"] == run["row_count"] + 1


def test_force_bypasses_the_cache(client, saved, target_sqlite):
    run = client.post(f"/queries/{saved['id']}/run").json()
    _insert_row(target_sqlite)
    forced = client.get(
        f"/queries/{saved['id']}/poll",
        params={"since_hash": run["data_hash"], "force": "true"},
    ).json()
    assert forced["changed"] is True
    assert forced["from_cache"] is False


def test_updating_a_query_invalidates_its_cache(client, saved):
    client.post(f"/queries/{saved['id']}/run")
    assert result_cache.get(saved["id"]) is not None
    client.put(f"/queries/{saved['id']}", json={"chart_type": "bar"})
    assert result_cache.get(saved["id"]) is None


def test_poll_on_missing_query_is_404(client):
    assert client.get("/queries/nope/poll").status_code == 404


def test_poll_uses_the_per_query_interval(client, sqlite_connection):
    created = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={
            "name": "fast",
            "sql_text": "SELECT day FROM txns",
            "poll_interval_ms": 1500,
        },
    ).json()
    assert client.get(f"/queries/{created['id']}/poll").json()["poll_interval_ms"] == 1500


# ---------------------------------------------------------------------------
# /preview
# ---------------------------------------------------------------------------


def test_preview_runs_without_saving(client, sqlite_connection, session):
    from app.models import SavedQuery

    r = client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT day, amount FROM txns ORDER BY id"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["day", "amount"]
    assert r.json()["row_count"] == 5
    assert session.query(SavedQuery).count() == 0


def test_preview_is_capped_at_the_preview_limit(client, sqlite_connection, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FAE_PREVIEW_ROW_LIMIT", "2")
    get_settings.cache_clear()
    r = client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT * FROM txns", "row_limit": 1000},
    )
    assert r.json()["row_count"] == 2
    assert r.json()["truncated"] is True


def test_preview_goes_through_the_guard(client, sqlite_connection):
    r = client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "DROP TABLE txns"},
    )
    assert r.status_code == 400
    assert r.json()["error_code"] == "NON_SELECT_STATEMENT"


def test_preview_writes_no_execution_log(client, sqlite_connection, session):
    client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT 1"},
    )
    assert session.query(QueryExecutionLog).count() == 0


def test_preview_on_missing_connection_is_404(client):
    r = client.post("/connections/nope/query/preview", json={"sql_text": "SELECT 1"})
    assert r.status_code == 404


def test_poll_reports_unchanged_after_a_cold_execution(client, saved):
    """The cold path must also honour since_hash, not only the cached path."""
    run = client.post(f"/queries/{saved['id']}/run").json()
    result_cache.clear()
    body = client.get(
        f"/queries/{saved['id']}/poll", params={"since_hash": run["data_hash"]}
    ).json()
    assert body["changed"] is False
    assert body["from_cache"] is False
    assert body["data_hash"] == run["data_hash"]
    assert "rows" not in body
