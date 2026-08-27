"""An analyst sees their own work and nobody else's."""

from __future__ import annotations

import pytest

from app.models.enums import UserRole
from tests.test_auth_api import login, make_user

PASSWORD = "a-perfectly-fine-password"


def _auth(client, email, role):
    make_user(email=email, role=role)
    return {"Authorization": f"Bearer {login(client, email=email).json()['token']}"}


@pytest.fixture
def alice(client, app_db):
    # example.com, not b.test: EmailStr (via email-validator) hard-rejects
    # every RFC 2606 special-use TLD -- .test among them -- even with
    # deliverability checking off, so a *.test address never survives
    # POST /auth/login's request-body validation. See the identical note on
    # the admin_client fixture in tests/conftest.py.
    return _auth(client, "alice@example.com", UserRole.ANALYST)


@pytest.fixture
def bob(client, app_db):
    return _auth(client, "bob@example.com", UserRole.ANALYST)


@pytest.fixture
def boss(client, app_db):
    return _auth(client, "boss@example.com", UserRole.ADMIN)


@pytest.fixture
def connection(client, boss, target_sqlite):
    return client.post(
        "/connections",
        headers=boss,
        json={"name": "shared", "db_type": "sqlite", "sqlite_path": target_sqlite},
    ).json()["connection"]


def _query(client, auth, connection, name):
    response = client.post(
        f"/connections/{connection['id']}/queries",
        headers=auth,
        json={"name": name, "sql_text": "SELECT day, count(*) AS n FROM txns GROUP BY day"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _flagged_query(client, auth, connection, name):
    """A query whose own SQL selects the raw rows, with a rule that catches
    two of the five seed transactions - the same shape
    tests/test_flag_rules_api.py uses to get real findings into the flagged
    store rather than an empty section.
    """
    response = client.post(
        f"/connections/{connection['id']}/queries",
        headers=auth,
        json={"name": name, "sql_text": "SELECT day, amount, comment FROM txns ORDER BY id"},
    )
    assert response.status_code == 201, response.text
    created = response.json()

    rules = client.put(
        f"/queries/{created['id']}/flag-rules",
        headers=auth,
        json={
            "rules": [
                {
                    "name": "Large",
                    "severity": "high",
                    "enabled": True,
                    "conditions": [
                        {"column_name": "amount", "operator": "gt", "value": "500"}
                    ],
                }
            ]
        },
    )
    assert rules.status_code == 200, rules.text

    run = client.post(f"/queries/{created['id']}/run", headers=auth)
    assert run.status_code == 200, run.text

    return created


def test_a_query_belongs_to_whoever_created_it(client, alice, connection):
    created = _query(client, alice, connection, "alice's")

    listed = client.get("/queries", headers=alice).json()
    assert [q["id"] for q in listed] == [created["id"]]


def test_an_analyst_does_not_see_another_analysts_queries(client, alice, bob, connection):
    _query(client, alice, connection, "alice's")

    assert client.get("/queries", headers=bob).json() == []


def test_an_analyst_cannot_fetch_another_analysts_query_by_id(client, alice, bob, connection):
    """Absence from a list is not protection: the id is guessable from a URL
    somebody pasted into chat."""
    created = _query(client, alice, connection, "alice's")

    response = client.get(f"/queries/{created['id']}", headers=bob)

    # 404, not 403. Confirming that an id exists but belongs to somebody else
    # tells Bob what Alice is working on.
    assert response.status_code == 404


def test_an_analyst_cannot_edit_another_analysts_query(client, alice, bob, connection):
    created = _query(client, alice, connection, "alice's")

    response = client.put(
        f"/queries/{created['id']}",
        headers=bob,
        json={"name": "bob's now"},
    )

    assert response.status_code == 404


def test_an_analyst_cannot_delete_another_analysts_query(client, alice, bob, connection):
    created = _query(client, alice, connection, "alice's")

    assert client.delete(f"/queries/{created['id']}", headers=bob).status_code == 404


def test_an_analyst_cannot_reach_another_analysts_charts(client, alice, bob, connection):
    created = _query(client, alice, connection, "alice's")

    response = client.get(f"/queries/{created['id']}/charts", headers=bob)

    assert response.status_code == 404


def test_an_analyst_cannot_run_another_analysts_query(client, alice, bob, connection):
    """The one that actually touches the customer's database."""
    created = _query(client, alice, connection, "alice's")

    assert client.get(f"/queries/{created['id']}/poll", headers=bob).status_code == 404


def test_an_admin_sees_every_analysts_work(client, alice, bob, boss, connection):
    _query(client, alice, connection, "alice's")
    _query(client, bob, connection, "bob's")

    listed = client.get("/queries", headers=boss).json()

    assert len(listed) == 2


def test_an_admin_can_open_an_analysts_query(client, alice, boss, connection):
    created = _query(client, alice, connection, "alice's")

    assert client.get(f"/queries/{created['id']}", headers=boss).status_code == 200


def test_running_a_query_records_who_ran_it(client, alice, connection):
    """The audit question this whole change exists to answer."""
    created = _query(client, alice, connection, "alice's")
    client.get(f"/queries/{created['id']}/poll?force=true", headers=alice)

    logs = client.get(f"/queries/{created['id']}/logs", headers=alice).json()

    assert logs
    assert logs[0]["user_id"] is not None


def test_dashboards_belong_to_their_creator(client, alice, bob):
    created = client.post("/dashboards", headers=alice, json={"name": "alice's board"})
    assert created.status_code == 201, created.text

    assert client.get("/dashboards", headers=bob).json() == []
    assert len(client.get("/dashboards", headers=alice).json()) == 1


def test_unowned_rows_are_invisible_to_analysts(client, alice, boss, connection, app_db):
    """Everything created before accounts existed has no owner. It must not
    become visible to whoever signs up first."""
    from app.db.app_state import get_sessionmaker
    from app.models.saved_query import SavedQuery

    db = get_sessionmaker()()
    try:
        db.add(
            SavedQuery(
                connection_id=connection["id"],
                name="legacy",
                sql_text="SELECT 1 AS n",
                owner_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    assert client.get("/queries", headers=alice).json() == []
    assert len(client.get("/queries", headers=boss).json()) == 1



def test_an_analyst_cannot_see_another_analysts_flagged_rows_on_a_shared_connection(
    client, alice, bob, connection
):
    """A connection is shared across every analyst who queries it, but the
    findings behind each query are not. Probed the way an attacker would: not
    by reading the code, but by asking the endpoint what it returns."""
    created = _flagged_query(client, alice, connection, "alice's flagged")

    # Alice sees her own findings through the connection-level view.
    own = client.get(f"/connections/{connection['id']}/flagged", headers=alice).json()
    assert [q["query_id"] for q in own["queries"]] == [created["id"]]
    assert own["flagged_count"] == 2

    # Bob shares the same connection and must see none of it: no section for
    # alice's query, no row values, no rule names, no count.
    bobs_view = client.get(f"/connections/{connection['id']}/flagged", headers=bob).json()
    assert bobs_view["queries"] == []
    assert bobs_view["flagged_count"] == 0

    # The refresh action is the same view with a side effect - it must not
    # run, or report on, a query Bob cannot see either.
    refreshed = client.post(
        f"/connections/{connection['id']}/flagged/refresh", headers=bob
    ).json()
    assert refreshed["queries"] == []
    assert refreshed["flagged_count"] == 0


def test_an_analyst_does_not_see_another_analysts_findings_in_the_summary(
    client, alice, bob, connection
):
    """The summary is the badge every page reads on load - a leak here is a
    leak on every navigation, not just one page."""
    created = _flagged_query(client, alice, connection, "alice's summary target")

    bobs_summary = client.get("/flagged/summary", headers=bob).json()
    assert created["id"] not in {q["query_id"] for q in bobs_summary["queries"]}
    assert bobs_summary["flagged_count"] == 0

    alices_summary = client.get("/flagged/summary", headers=alice).json()
    assert created["id"] in {q["query_id"] for q in alices_summary["queries"]}
    assert alices_summary["flagged_count"] == 2


def test_an_analyst_cannot_attach_another_analysts_chart_to_a_dashboard(
    client, alice, bob, connection
):
    """The row-data path already 404s bob out of alice's query (see
    test_an_analyst_cannot_run_another_analysts_query). This is the same
    check for the reference itself: attaching a chart id must not become a
    way to learn, by 201 versus 404, which otherwise-unguessable chart ids
    exist."""
    created = _query(client, alice, connection, "alice's chart source")
    chart_id = created["charts"][0]["id"]

    response = client.post(
        "/dashboards",
        headers=bob,
        json={"name": "bob's board", "chart_ids": [chart_id]},
    )

    assert response.status_code == 404
