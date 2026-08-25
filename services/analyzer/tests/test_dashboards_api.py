"""Dashboard CRUD, ordering, and the referential integrity that keeps boards honest."""

from __future__ import annotations

import pytest

from app.models import Dashboard, DashboardItem


@pytest.fixture
def make_saved_query(client, sqlite_connection):
    """Create a saved query on the temp SQLite target and return its id."""
    counter = {"n": 0}

    owners: dict[str, str] = {}

    def _make(name: str | None = None) -> str:
        """Create a query and return the id of its chart.

        A dashboard places a *chart*, not a query - that is what lets one
        result appear on a board as both a line and a table while the SQL runs
        once. Creating a query seeds one table chart, which these tests then
        configure.
        """
        counter["n"] += 1
        response = client.post(
            f"/connections/{sqlite_connection['id']}/queries",
            json={
                "name": name or f"query {counter['n']}",
                "sql_text": "SELECT day, count(*) AS n FROM txns GROUP BY day",
            },
        )
        assert response.status_code == 201, response.text
        query_id = response.json()["id"]

        charts = client.put(
            f"/queries/{query_id}/charts",
            json={
                "charts": [
                    {
                        "name": "Trend",
                        "chart_type": "line",
                        "x_field": "day",
                        "y_field": "n",
                    }
                ]
            },
        )
        assert charts.status_code == 200, charts.text
        chart_id = charts.json()["charts"][0]["id"]
        owners[chart_id] = query_id
        return chart_id

    _make.query_of = owners.__getitem__  # type: ignore[attr-defined]
    return _make


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_empty_dashboard(client):
    r = client.post("/dashboards", json={"name": "Card testing"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Card testing"
    assert body["chart_ids"] == []
    assert body["id"]
    assert body["created_at"] and body["updated_at"]


def test_create_populated_in_one_call(client, make_saved_query):
    first, second = make_saved_query(), make_saved_query()
    r = client.post(
        "/dashboards", json={"name": "Chargebacks", "chart_ids": [second, first]}
    )
    assert r.status_code == 201, r.text
    # Order is as given, not as created.
    assert r.json()["chart_ids"] == [second, first]


def test_create_rejects_an_unknown_query_and_persists_nothing(client, session):
    r = client.post(
        "/dashboards", json={"name": "Bad", "chart_ids": ["does-not-exist"]}
    )
    assert r.status_code == 404
    assert r.json()["error_code"] == "QUERY_NOT_FOUND"
    # A board pointing at a missing query would render a card that can only error.
    assert session.query(Dashboard).count() == 0


def test_create_collapses_duplicate_ids(client, make_saved_query):
    query_id = make_saved_query()
    r = client.post(
        "/dashboards", json={"name": "Dupes", "chart_ids": [query_id, query_id]}
    )
    assert r.status_code == 201
    assert r.json()["chart_ids"] == [query_id]


def test_create_rejects_a_duplicate_name(client):
    client.post("/dashboards", json={"name": "Only one"})
    r = client.post("/dashboards", json={"name": "Only one"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_create_rejects_a_blank_name(client):
    r = client.post("/dashboards", json={"name": ""})
    assert r.status_code == 422
    assert r.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


def test_create_rejects_a_name_that_is_too_long(client):
    r = client.post("/dashboards", json={"name": "x" * 201})
    assert r.status_code == 422
    assert r.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_list_is_empty_before_anything_is_created(client):
    r = client.get("/dashboards")
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_oldest_first(client):
    for name in ("first", "second", "third"):
        assert client.post("/dashboards", json={"name": name}).status_code == 201
    assert [d["name"] for d in client.get("/dashboards").json()] == [
        "first",
        "second",
        "third",
    ]


def test_get_one_by_id(client, make_saved_query):
    query_id = make_saved_query()
    created = client.post(
        "/dashboards", json={"name": "Board", "chart_ids": [query_id]}
    ).json()

    r = client.get(f"/dashboards/{created['id']}")
    assert r.status_code == 200
    assert r.json()["chart_ids"] == [query_id]


def test_get_unknown_dashboard_is_404(client):
    r = client.get("/dashboards/nope")
    assert r.status_code == 404
    assert r.json()["error_code"] == "DASHBOARD_NOT_FOUND"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_rename_leaves_the_arrangement_alone(client, make_saved_query):
    query_id = make_saved_query()
    created = client.post(
        "/dashboards", json={"name": "Before", "chart_ids": [query_id]}
    ).json()

    r = client.put(f"/dashboards/{created['id']}", json={"name": "After"})
    assert r.status_code == 200
    assert r.json()["name"] == "After"
    assert r.json()["chart_ids"] == [query_id]


def test_query_ids_replace_rather_than_merge(client, make_saved_query):
    first, second, third = make_saved_query(), make_saved_query(), make_saved_query()
    created = client.post(
        "/dashboards", json={"name": "Board", "chart_ids": [first, second]}
    ).json()

    r = client.put(f"/dashboards/{created['id']}", json={"chart_ids": [third]})
    assert r.status_code == 200
    assert r.json()["chart_ids"] == [third]


def test_reordering_is_an_update(client, make_saved_query):
    first, second = make_saved_query(), make_saved_query()
    created = client.post(
        "/dashboards", json={"name": "Board", "chart_ids": [first, second]}
    ).json()

    r = client.put(f"/dashboards/{created['id']}", json={"chart_ids": [second, first]})
    assert r.json()["chart_ids"] == [second, first]


def test_emptying_a_dashboard(client, make_saved_query):
    created = client.post(
        "/dashboards", json={"name": "Board", "chart_ids": [make_saved_query()]}
    ).json()

    r = client.put(f"/dashboards/{created['id']}", json={"chart_ids": []})
    assert r.json()["chart_ids"] == []


def test_update_rejects_an_unknown_query_and_changes_nothing(
    client, make_saved_query
):
    query_id = make_saved_query()
    created = client.post(
        "/dashboards", json={"name": "Board", "chart_ids": [query_id]}
    ).json()

    r = client.put(f"/dashboards/{created['id']}", json={"chart_ids": ["ghost"]})
    assert r.status_code == 404
    assert r.json()["error_code"] == "QUERY_NOT_FOUND"
    assert client.get(f"/dashboards/{created['id']}").json()["chart_ids"] == [query_id]


def test_update_rejects_a_name_already_taken(client):
    client.post("/dashboards", json={"name": "taken"})
    other = client.post("/dashboards", json={"name": "other"}).json()

    r = client.put(f"/dashboards/{other['id']}", json={"name": "taken"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_update_unknown_dashboard_is_404(client):
    r = client.put("/dashboards/nope", json={"name": "x"})
    assert r.status_code == 404
    assert r.json()["error_code"] == "DASHBOARD_NOT_FOUND"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_the_board(client):
    created = client.post("/dashboards", json={"name": "Temporary"}).json()

    assert client.delete(f"/dashboards/{created['id']}").status_code == 204
    assert client.get(f"/dashboards/{created['id']}").status_code == 404


def test_delete_leaves_the_saved_queries_alone(client, make_saved_query):
    chart_id = make_saved_query()
    created = client.post(
        "/dashboards", json={"name": "Temporary", "chart_ids": [chart_id]}
    ).json()

    client.delete(f"/dashboards/{created['id']}")
    # The board was an arrangement; the queries belong to their connection.
    assert client.get(f"/queries/{make_saved_query.query_of(chart_id)}").status_code == 200


def test_delete_unknown_dashboard_is_404(client):
    assert client.delete("/dashboards/nope").status_code == 404


# ---------------------------------------------------------------------------
# referential integrity
# ---------------------------------------------------------------------------


def test_deleting_a_query_takes_it_off_every_dashboard(
    client, make_saved_query, session
):
    """The reason membership is a table and not a JSON blob of ids."""
    doomed, kept = make_saved_query(), make_saved_query()
    first = client.post(
        "/dashboards", json={"name": "One", "chart_ids": [doomed, kept]}
    ).json()
    second = client.post(
        "/dashboards", json={"name": "Two", "chart_ids": [doomed]}
    ).json()

    # Deleting the query cascades to its charts, which cascades to every board
    # placing one of them - two foreign keys deep, and the point of both.
    assert client.delete(f"/queries/{make_saved_query.query_of(doomed)}").status_code == 204

    assert client.get(f"/dashboards/{first['id']}").json()["chart_ids"] == [kept]
    assert client.get(f"/dashboards/{second['id']}").json()["chart_ids"] == []
    assert session.query(DashboardItem).filter_by(chart_id=doomed).count() == 0


def test_deleting_a_connection_takes_its_queries_off_dashboards(
    client, sqlite_connection, make_saved_query
):
    query_id = make_saved_query()
    board = client.post(
        "/dashboards", json={"name": "Board", "chart_ids": [query_id]}
    ).json()

    assert client.delete(f"/connections/{sqlite_connection['id']}").status_code == 204

    # The cascade runs connection -> saved query -> dashboard item.
    assert client.get(f"/dashboards/{board['id']}").json()["chart_ids"] == []


def test_a_dashboard_can_span_connections(client, make_saved_query, target_sqlite):
    """The whole point of a dashboard: one view over more than one database."""
    first = make_saved_query()

    other = client.post(
        "/connections",
        json={"name": "second target", "db_type": "sqlite", "sqlite_path": target_sqlite},
    ).json()["connection"]
    second_query = client.post(
        f"/connections/{other['id']}/queries",
        json={"name": "elsewhere", "sql_text": "SELECT count(*) AS n FROM txns"},
    ).json()["id"]
    # Creating a query seeds one table chart; a board places that.
    second = client.get(f"/queries/{second_query}/charts").json()["charts"][0]["id"]

    r = client.post(
        "/dashboards", json={"name": "Cross", "chart_ids": [first, second]}
    )
    assert r.status_code == 201
    assert r.json()["chart_ids"] == [first, second]
