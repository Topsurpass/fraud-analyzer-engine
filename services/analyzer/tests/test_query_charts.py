"""Several charts from one query, and the execution saving that justifies it.

The complaint this answers: showing one result three ways meant saving the
query three times, and because the result cache is keyed by query id, the
engine then ran identical SQL three times against the customer's database on
every poll. These tests are mostly about proving that stopped.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def query(admin_client, sqlite_connection):
    created = admin_client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={
            "name": "per day",
            "sql_text": "SELECT day, count(*) AS n FROM txns GROUP BY day ORDER BY day",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def _charts(admin_client, query_id, charts):
    response = admin_client.put(f"/queries/{query_id}/charts", json={"charts": charts})
    assert response.status_code == 200, response.text
    return response.json()["charts"]


def test_three_charts_cost_one_execution(admin_client, query):
    """The whole point.

    Three views of one result used to be three saved queries, three cache
    entries and three round trips to the target on every poll.
    """
    _charts(
        admin_client,
        query["id"],
        [
            {"name": "Trend", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Bars", "chart_type": "bar", "x_field": "day", "y_field": "n"},
            {"name": "Rows", "chart_type": "table"},
        ],
    )

    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())
    body = admin_client.post(f"/queries/{query['id']}/run").json()
    after = len(admin_client.get(f"/queries/{query['id']}/logs").json())

    assert len(body["charts"]) == 3
    # One execution logged, not three.
    assert after - before == 1


def test_every_chart_travels_on_the_one_payload(admin_client, query):
    _charts(
        admin_client,
        query["id"],
        [
            {"name": "Trend", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Rows", "chart_type": "table"},
        ],
    )
    body = admin_client.post(f"/queries/{query['id']}/run").json()

    assert [chart["name"] for chart in body["charts"]] == ["Trend", "Rows"]
    assert [chart["type"] for chart in body["charts"]] == ["line", "table"]
    # One set of rows for all of them: that is what makes the saving real.
    assert body["row_count"] == len(body["rows"])
    assert body["row_count"] > 0


def test_each_chart_is_identified_on_the_payload(admin_client, query):
    # A dashboard places one of several, so a spec without an id cannot be
    # matched to the placement that asked for it.
    charts = _charts(admin_client, query["id"], [{"name": "Rows"}])
    body = admin_client.post(f"/queries/{query['id']}/run").json()
    assert body["charts"][0]["id"] == charts[0]["id"]


def test_a_warning_is_reported_per_chart(admin_client, query):
    """One bad mapping must not make the others look broken."""
    _charts(
        admin_client,
        query["id"],
        [
            {"name": "Good", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Bad", "chart_type": "line", "x_field": "day", "y_field": "gone"},
        ],
    )
    body = admin_client.post(f"/queries/{query['id']}/run").json()
    by_name = {chart["name"]: chart for chart in body["charts"]}

    assert by_name["Good"]["warnings"] == []
    assert any("gone" in warning for warning in by_name["Bad"]["warnings"])
    # Still returns the rows: a bad mapping is a warning, never a failed run.
    assert body["row_count"] > 0


def test_a_chart_can_be_placed_on_a_dashboard(admin_client, query):
    charts = _charts(
        admin_client,
        query["id"],
        [
            {"name": "Trend", "chart_type": "line", "x_field": "day", "y_field": "n"},
            {"name": "Rows", "chart_type": "table"},
        ],
    )
    board = admin_client.post(
        "/dashboards",
        json={"name": "Both", "chart_ids": [charts[0]["id"], charts[1]["id"]]},
    )
    assert board.status_code == 201, board.text
    # The same result, twice on one board, for one execution.
    assert board.json()["chart_ids"] == [charts[0]["id"], charts[1]["id"]]


def test_deleting_a_chart_takes_it_off_its_dashboards(admin_client, query):
    charts = _charts(admin_client, query["id"], [{"name": "Trend"}, {"name": "Rows"}])
    board = admin_client.post(
        "/dashboards",
        json={"name": "Both", "chart_ids": [charts[0]["id"], charts[1]["id"]]},
    ).json()

    _charts(admin_client, query["id"], [{"name": "Rows"}])
    assert admin_client.get(f"/dashboards/{board['id']}").json()["chart_ids"] == [
        charts[1]["id"]
    ]


def test_deleting_the_query_deletes_its_charts(admin_client, query):
    _charts(admin_client, query["id"], [{"name": "Trend"}])
    assert admin_client.delete(f"/queries/{query['id']}").status_code == 204
    assert admin_client.get(f"/queries/{query['id']}/charts").status_code == 404


def test_charts_on_an_unknown_query_are_404(admin_client):
    assert admin_client.get("/queries/nope/charts").status_code == 404
    assert admin_client.put("/queries/nope/charts", json={"charts": []}).status_code == 404


def test_a_query_may_have_no_charts_at_all(admin_client, query):
    # Legitimate mid-edit state, and it must not fail the run.
    assert _charts(admin_client, query["id"], []) == []
    body = admin_client.post(f"/queries/{query['id']}/run").json()
    assert body["charts"] == []
    assert body["row_count"] > 0


def test_a_chart_keeps_the_threshold_it_was_saved_with(admin_client, query):
    """The threshold is per chart because sensitivity is per chart: a card
    watching one busy terminal and a card watching a long tail do not want the
    same number."""
    response = admin_client.put(
        f"/queries/{query['id']}/charts",
        json={
            "charts": [
                {
                    "name": "Sensitive",
                    "chart_type": "compare_grid",
                    "x_field": "bucket",
                    "y_field": "amount",
                    "series_field": "terminal",
                    "surge_threshold_pct": 25,
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["charts"][0]["surge_threshold_pct"] == 25

    # And it survives a read, not just the write's echo.
    listed = admin_client.get(f"/queries/{query['id']}/charts")
    assert listed.json()["charts"][0]["surge_threshold_pct"] == 25


def test_a_chart_saved_without_a_threshold_reports_none(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/charts",
        json={"charts": [{"name": "Default", "chart_type": "line"}]},
    )
    assert response.json()["charts"][0]["surge_threshold_pct"] is None


def test_a_threshold_of_zero_is_refused(admin_client, query):
    """Zero would flag every movement including none at all, which is the same
    as having no threshold while looking like a configured one."""
    response = admin_client.put(
        f"/queries/{query['id']}/charts",
        json={"charts": [{"name": "Bad", "chart_type": "line", "surge_threshold_pct": 0}]},
    )
    assert response.status_code == 422


def test_a_negative_threshold_is_refused(admin_client, query):
    """The threshold is a magnitude covering both directions, so a sign on it
    is a misunderstanding worth refusing rather than silently reinterpreting."""
    response = admin_client.put(
        f"/queries/{query['id']}/charts",
        json={"charts": [{"name": "Bad", "chart_type": "line", "surge_threshold_pct": -50}]},
    )
    assert response.status_code == 422


def test_the_threshold_reaches_the_client_on_the_run_payload(admin_client, query):
    """The response model is a second place the field has to exist.

    It did not, once: ``build_chart`` resolved the threshold correctly and
    Pydantic dropped it on the way out, because ``ChartSpec`` listed every
    field explicitly and this one was not among them. The chart editor showed
    the saved number while every chart on screen judged against the default,
    and nothing anywhere reported a problem.
    """
    _charts(
        admin_client,
        query["id"],
        [
            {
                "name": "Tight",
                "chart_type": "compare_grid",
                "x_field": "day",
                "y_field": "n",
                "series_field": "day",
                "surge_threshold_pct": 120,
            }
        ],
    )

    payload = admin_client.get(f"/queries/{query['id']}/poll?force=true").json()
    assert payload["charts"][0]["surge_threshold_pct"] == 120


def test_an_unset_threshold_arrives_resolved_rather_than_null(admin_client, query):
    """A caller must never have to know what the default is."""
    _charts(admin_client, query["id"], [{"name": "Loose", "chart_type": "line", "x_field": "day", "y_field": "n"}])

    payload = admin_client.get(f"/queries/{query['id']}/poll?force=true").json()
    assert payload["charts"][0]["surge_threshold_pct"] == 50.0
