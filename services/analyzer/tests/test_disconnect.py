"""Disconnecting a database, and what that has to mean to be worth having.

"Disconnected" that silently reconnects on the next poll is not disconnected.
These tests are about the difference: the pooled sockets close, the scheduler
stops, every execution path refuses, and nothing the analyst built is lost.
"""

from __future__ import annotations

import pytest

from tests.test_flag_rules_api import make_query, rule


@pytest.fixture
def query(admin_client, sqlite_connection):
    return make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns",
        name="watched",
    )


def test_disconnecting_marks_it_paused(admin_client, sqlite_connection):
    r = admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True


def test_the_last_test_result_is_preserved(admin_client, sqlite_connection):
    """Status records how the last test went.

    Folding "I turned this off" into it would destroy the answer to "was this
    working when I paused it", which is the first thing anyone asks on
    reconnecting.
    """
    assert sqlite_connection["status"] == "ok"
    paused = admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect").json()
    assert paused["status"] == "ok"


def test_running_a_query_on_a_disconnected_database_is_refused(
    admin_client, sqlite_connection, query
):
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    r = admin_client.post(f"/queries/{query['id']}/run")
    assert r.status_code == 409
    assert r.json()["error_code"] == "CONNECTION_PAUSED"
    # The message has to name the fix, not just the state.
    assert "reconnect" in r.json()["message"].lower()


def test_polling_is_refused_too(admin_client, sqlite_connection, query):
    # Otherwise a dashboard left open would keep the connection alive by
    # accident, which is the failure mode this feature exists to prevent.
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    assert admin_client.get(f"/queries/{query['id']}/poll", params={"force": True}).status_code == 409


def test_preview_is_refused_too(admin_client, sqlite_connection):
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    r = admin_client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT 1 AS n"},
    )
    assert r.status_code == 409


def test_the_scheduler_skips_a_disconnected_connection(admin_client, sqlite_connection, query):
    from sqlalchemy.orm import Session

    from app.db.app_state import get_engine
    from app.services import scheduler

    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    scheduler.reset()
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")

    with Session(get_engine()) as session:
        # Skipped rather than failed: this is not an error to back off from,
        # and it must not fill the log every tick while a connection is off.
        assert scheduler.run_due_once(session) == 0


def test_reconnecting_puts_it_back_and_tests_it(admin_client, sqlite_connection, query):
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    r = admin_client.post(f"/connections/{sqlite_connection['id']}/reconnect")

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert admin_client.post(f"/queries/{query['id']}/run").status_code == 200


def test_reconnecting_reports_a_target_that_broke_while_it_was_off(
    admin_client, sqlite_connection, monkeypatch
):
    """Tested rather than trusted.

    A connection paused for a week may have had its password rotated or its
    host moved. Reporting "connected" without checking only moves the failure
    to the next scheduled run, where nobody is watching.
    """
    from app.errors import DbUnreachableError

    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")

    def unreachable(*_args, **_kwargs):
        raise DbUnreachableError("target went away")

    monkeypatch.setattr("app.db.target_registry.probe", unreachable)
    r = admin_client.post(f"/connections/{sqlite_connection['id']}/reconnect")
    assert r.json()["ok"] is False
    assert r.json()["status"] == "failed"


def test_nothing_is_lost_while_disconnected(admin_client, sqlite_connection, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    admin_client.post(f"/queries/{query['id']}/run")
    before = admin_client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert before["flagged_count"] > 0

    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")

    # Queries, rules and findings all survive: this is a pause, not a delete.
    assert admin_client.get(f"/queries/{query['id']}").status_code == 200
    assert len(admin_client.get(f"/queries/{query['id']}/flag-rules").json()["rules"]) == 1
    after = admin_client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert after["flagged_count"] == before["flagged_count"]


def test_a_new_connection_is_not_paused(admin_client, sqlite_connection):
    assert sqlite_connection["paused"] is False


def test_disconnecting_an_unknown_connection_is_404(admin_client):
    assert admin_client.post("/connections/nope/disconnect").status_code == 404
    assert admin_client.post("/connections/nope/reconnect").status_code == 404


def test_disconnecting_twice_is_harmless(admin_client, sqlite_connection):
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    r = admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    assert r.status_code == 200
    assert r.json()["paused"] is True
