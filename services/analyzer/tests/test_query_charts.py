"""Several charts from one query, and the execution saving that justifies it.

The complaint this answers: showing one result three ways meant saving the
query three times, and because the result cache is keyed by query id, the
engine then ran identical SQL three times against the customer's database on
every poll. These tests are mostly about proving that stopped.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def query(client, sqlite_connection):
    created = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={
            "name": "per day",
            "sql_text": "SELECT day, count(*) AS n FROM txns GROUP BY day ORDER BY day",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def _charts(client, query_id, charts):
    response = client.put(f"/queries/{query_id}/charts", json={"charts": charts})
    assert response.status_code == 200, response.text
    return response.json()["charts"]


def test_three_charts_cost_one_execution(client, query):
    """The whole point.

    Three views of one result used to be three saved queries, three cache
    entries and three round trips to the target on every poll.
    """
    _charts(
        client,
        query["id"],
        [
            {"name": "Trend", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Bars", "chart_type": "bar", "x_field": "day", "y_field": "n"},
            {"name": "Rows", "chart_type": "table"},
        ],
    )

    before = len(client.get(f"/queries/{query['id']}/logs").json())
    body = client.post(f"/queries/{query['id']}/run").json()
    after = len(client.get(f"/queries/{query['id']}/logs").json())

    assert len(body["charts"]) == 3
    # One execution logged, not three.
    assert after - before == 1


def test_every_chart_travels_on_the_one_payload(client, query):
    _charts(
        client,
        query["id"],
        [
            {"name": "Trend", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Rows", "chart_type": "table"},
        ],
    )
    body = client.post(f"/queries/{query['id']}/run").json()

    assert [chart["name"] for chart in body["charts"]] == ["Trend", "Rows"]
    assert [chart["type"] for chart in body["charts"]] == ["line", "table"]
    # One set of rows for all of them: that is what makes the saving real.
    assert body["row_count"] == len(body["rows"])
    assert body["row_count"] > 0


def test_each_chart_is_identified_on_the_payload(client, query):
    # A dashboard places one of several, so a spec without an id cannot be
    # matched to the placement that asked for it.
    charts = _charts(client, query["id"], [{"name": "Rows"}])
    body = client.post(f"/queries/{query['id']}/run").json()
    assert body["charts"][0]["id"] == charts[0]["id"]


def test_a_warning_is_reported_per_chart(client, query):
    """One bad mapping must not make the others look broken."""
    _charts(
        client,
        query["id"],
        [
            {"name": "Good", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Bad", "chart_type": "line", "x_field": "day", "y_field": "gone"},
        ],
    )
    body = client.post(f"/queries/{query['id']}/run").json()
    by_name = {chart["name"]: chart for chart in body["charts"]}

    assert by_name["Good"]["warnings"] == []
    assert any("gone" in warning for warning in by_name["Bad"]["warnings"])
    # Still returns the rows: a bad mapping is a warning, never a failed run.
    assert body["row_count"] > 0


def test_a_chart_can_be_placed_on_a_dashboard(client, query):
    charts = _charts(
        client,
        query["id"],
        [
            {"name": "Trend", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Rows", "chart_type": "table"},
        ],
    )
    board = client.post(
        "/dashboards",
        json={"name": "Both", "chart_ids": [charts[0]["id"], charts[1]["id"]]},
    )
    assert board.status_code == 201, board.text
    # The same result, twice on one board, for one execution.
    assert board.json()["chart_ids"] == [charts[0]["id"], charts[1]["id"]]


def test_deleting_a_chart_takes_it_off_its_dashboards(client, query):
    charts = _charts(client, query["id"], [{"name": "Trend"}, {"name": "Rows"}])
    board = client.post(
        "/dashboards",
        json={"name": "Both", "chart_ids": [charts[0]["id"], charts[1]["id"]]},
    ).json()

    _charts(client, query["id"], [{"name": "Rows"}])
    assert client.get(f"/dashboards/{board['id']}").json()["chart_ids"] == [
        charts[1]["id"]
    ]


def test_deleting_the_query_deletes_its_charts(client, query):
    _charts(client, query["id"], [{"name": "Trend"}])
    assert client.delete(f"/queries/{query['id']}").status_code == 204
    assert client.get(f"/queries/{query['id']}/charts").status_code == 404


def test_charts_on_an_unknown_query_are_404(client):
    assert client.get("/queries/nope/charts").status_code == 404
    assert client.put("/queries/nope/charts", json={"charts": []}).status_code == 404


def test_a_query_may_have_no_charts_at_all(client, query):
    # Legitimate mid-edit state, and it must not fail the run.
    assert _charts(client, query["id"], []) == []
    body = client.post(f"/queries/{query['id']}/run").json()
    assert body["charts"] == []
    assert body["row_count"] > 0
