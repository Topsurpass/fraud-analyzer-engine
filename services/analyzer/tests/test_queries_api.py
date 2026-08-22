"""Saved-query CRUD, validation on save, and the dry run that gates persistence."""

from __future__ import annotations

import pytest

from app.models import SavedQuery


@pytest.fixture
def make_query(client, sqlite_connection):
    def _make(**over):
        payload = {
            "name": "flagged per day",
            "sql_text": (
                "SELECT day, count(*) AS flagged_count FROM txns "
                "WHERE flagged = 1 GROUP BY day ORDER BY day"
            ),
            "chart_type": "line",
            "x_field": "day",
            "y_field": "flagged_count",
        }
        payload.update(over)
        return client.post(
            f"/connections/{sqlite_connection['id']}/queries", json=payload
        )

    return _make


def test_save_valid_query(make_query):
    r = make_query()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "flagged per day"
    assert body["chart_type"] == "line"
    assert body["row_limit"] == 1000


def test_save_rejects_non_select_and_persists_nothing(make_query, session):
    r = make_query(sql_text="DROP TABLE txns")
    assert r.status_code == 400
    assert r.json()["error_code"] == "NON_SELECT_STATEMENT"
    assert session.query(SavedQuery).count() == 0


def test_save_rejects_multi_statement(make_query, session):
    r = make_query(sql_text="SELECT 1; DROP TABLE txns")
    assert r.status_code == 400
    assert r.json()["error_code"] == "MULTIPLE_STATEMENTS"
    assert session.query(SavedQuery).count() == 0


def test_save_rejects_forbidden_function(make_query, session):
    r = make_query(sql_text="SELECT readfile('/etc/passwd')")
    assert r.status_code == 400
    assert r.json()["error_code"] == "FORBIDDEN_FUNCTION"
    assert session.query(SavedQuery).count() == 0


def test_dry_run_rejects_sql_that_is_safe_but_wrong(make_query, session):
    r = make_query(sql_text="SELECT * FROM no_such_table")
    assert r.status_code == 400
    assert r.json()["error_code"] == "QUERY_EXECUTION_ERROR"
    assert session.query(SavedQuery).count() == 0


def test_dry_run_rejects_a_missing_column(make_query, session):
    r = make_query(sql_text="SELECT no_such_column FROM txns")
    assert r.status_code == 400
    assert session.query(SavedQuery).count() == 0


def test_row_limit_over_ceiling_rejected(make_query, session):
    r = make_query(row_limit=999_999)
    assert r.status_code == 400
    assert r.json()["error_code"] == "ROW_LIMIT_EXCEEDED"
    assert session.query(SavedQuery).count() == 0


def test_row_limit_at_ceiling_accepted(make_query):
    assert make_query(row_limit=10_000).status_code == 201


def test_duplicate_name_on_same_connection_is_409(make_query):
    assert make_query().status_code == 201
    r = make_query()
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_list_queries(make_query, client, sqlite_connection):
    make_query(name="a")
    make_query(name="b")
    r = client.get(f"/connections/{sqlite_connection['id']}/queries")
    assert r.status_code == 200
    assert [q["name"] for q in r.json()] == ["a", "b"]


def test_list_on_missing_connection_is_404(client):
    assert client.get("/connections/nope/queries").status_code == 404


def test_get_query(make_query, client):
    query_id = make_query().json()["id"]
    assert client.get(f"/queries/{query_id}").json()["id"] == query_id


def test_get_missing_query_is_404(client):
    r = client.get("/queries/nope")
    assert r.status_code == 404
    assert r.json()["error_code"] == "QUERY_NOT_FOUND"


def test_update_changes_chart_mapping(make_query, client):
    query_id = make_query().json()["id"]
    r = client.put(f"/queries/{query_id}", json={"chart_type": "bar"})
    assert r.status_code == 200
    assert r.json()["chart_type"] == "bar"


def test_update_revalidates_new_sql(make_query, client):
    query_id = make_query().json()["id"]
    r = client.put(f"/queries/{query_id}", json={"sql_text": "DELETE FROM txns"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "NON_SELECT_STATEMENT"
    # The original SQL survives a rejected update.
    assert "count(*)" in client.get(f"/queries/{query_id}").json()["sql_text"]


def test_update_dry_runs_new_sql(make_query, client):
    query_id = make_query().json()["id"]
    r = client.put(f"/queries/{query_id}", json={"sql_text": "SELECT * FROM ghost"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "QUERY_EXECUTION_ERROR"


def test_delete_query(make_query, client):
    query_id = make_query().json()["id"]
    assert client.delete(f"/queries/{query_id}").status_code == 204
    assert client.get(f"/queries/{query_id}").status_code == 404


def test_deleting_connection_cascades_to_queries(make_query, client, sqlite_connection):
    query_id = make_query().json()["id"]
    assert client.delete(f"/connections/{sqlite_connection['id']}").status_code == 204
    assert client.get(f"/queries/{query_id}").status_code == 404


def test_saving_against_missing_connection_is_404(client):
    r = client.post("/connections/nope/queries", json={"name": "x", "sql_text": "SELECT 1"})
    assert r.status_code == 404


def test_update_row_limit_is_clamped_against_the_ceiling(make_query, client):
    query_id = make_query().json()["id"]
    r = client.put(f"/queries/{query_id}", json={"row_limit": 999_999})
    assert r.status_code == 400
    assert r.json()["error_code"] == "ROW_LIMIT_EXCEEDED"


def test_update_row_limit_accepts_a_valid_value(make_query, client):
    query_id = make_query().json()["id"]
    assert client.put(f"/queries/{query_id}", json={"row_limit": 50}).json()["row_limit"] == 50


def test_renaming_onto_an_existing_name_is_409(make_query, client):
    make_query(name="first")
    second = make_query(name="second").json()
    r = client.put(f"/queries/{second['id']}", json={"name": "first"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_execution_log_failure_does_not_break_the_run(make_query, client, monkeypatch):
    """A logging failure must never turn a successful run into an error."""
    from app.services import saved_query_service

    query_id = make_query().json()["id"]

    def _explode(*_args, **_kwargs):
        raise RuntimeError("log table is on fire")

    monkeypatch.setattr(
        saved_query_service.Session, "add", _explode, raising=False
    )
    r = client.post(f"/queries/{query_id}/run")
    assert r.status_code == 200
    assert r.json()["row_count"] == 2
