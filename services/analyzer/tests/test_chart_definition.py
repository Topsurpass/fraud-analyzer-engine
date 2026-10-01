"""Reading the query behind a published chart, and only reading it.

A viewer may want to replicate a chart, so publishing now shares its definition.
What it must never share is the way to reach the database or the means to change
anything: no connection details, no list items, no write endpoint.
"""

from __future__ import annotations

import pytest

from app.enums import UserRole
from tests.test_chart_publishing import SQL, _approved, _auth
from tests.test_flag_rules_api import rule
from tests.test_lists_api import list_rule


@pytest.fixture
def people(client, app_db):
    return {
        "boss": _auth(client, "boss@example.com", UserRole.ADMIN),
        "alice": _auth(client, "alice@example.com", UserRole.ANALYST),
        "bob": _auth(client, "bob@example.com", UserRole.ANALYST),
    }


@pytest.fixture
def shared(client, people, sqlite_connection):
    """Alice's query with two rules (one on a list) and two charts, one of them published."""
    watch = client.post(
        "/lists",
        headers=people["boss"],
        json={"name": "Secret watchlist", "items": ["TOP-SECRET-ITEM-1", "TOP-SECRET-ITEM-2"]},
    ).json()
    query = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        headers=people["alice"],
        json={
            "name": "Daily volume",
            "description": "Transactions by day",
            "sql_text": SQL,
            "row_limit": 250,
            "poll_interval_ms": 3_600_000,
        },
    ).json()
    put = client.put(
        f"/queries/{query['id']}/flag-rules",
        headers=people["alice"],
        json={"rules": [
            rule("Busy day", "n", "gt", "3", severity="high"),
            list_rule("Watched", "day", "in_list", watch["id"]),
        ]},
    )
    assert put.status_code == 200, put.text
    charts = client.put(
        f"/queries/{query['id']}/charts",
        headers=people["alice"],
        json={"charts": [
            {"name": "Volume", "chart_type": "bar", "x_field": "day", "y_field": "n"},
            {"name": "Private one", "chart_type": "table"},
        ]},
    ).json()["charts"]
    return query, charts, watch


def _get(client, headers, chart_id):
    return client.get(f"/queries/charts/{chart_id}/definition", headers=headers)


def test_a_viewer_reads_the_definition_of_a_published_chart(client, people, shared):
    query, charts, _ = shared
    _approved(client, charts[0], people["alice"])

    response = _get(client, people["bob"], charts[0]["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == {
        "id": query["id"],
        "name": "Daily volume",
        "description": "Transactions by day",
        "sql_text": SQL,
        "row_limit": 250,
        "poll_interval_ms": 3_600_000,
    }
    assert body["chart"]["id"] == charts[0]["id"]
    assert body["chart"]["chart_type"] == "bar"
    assert (body["chart"]["x_field"], body["chart"]["y_field"]) == ("day", "n")
    assert body["owner_name"] == "An Analyst"
    assert body["connection_name"] == "target"
    assert body["read_only"] is True


def test_the_rules_come_with_their_conditions(client, people, shared):
    _, charts, _ = shared
    _approved(client, charts[0], people["alice"])

    rules = _get(client, people["bob"], charts[0]["id"]).json()["rules"]

    assert [r["name"] for r in rules] == ["Busy day", "Watched"]
    first, second = rules
    assert first["severity"] == "high"
    assert first["enabled"] is True
    assert first["conditions"] == [
        {"column_name": "n", "operator": "gt", "value": "3", "value2": None, "list_name": None}
    ]
    assert second["conditions"][0]["operator"] == "in_list"
    assert second["conditions"][0]["list_name"] == "Secret watchlist"


def test_a_list_condition_names_the_list_and_never_its_items(client, people, shared):
    _, charts, _ = shared
    _approved(client, charts[0], people["alice"])
    text = _get(client, people["bob"], charts[0]["id"]).text
    assert "Secret watchlist" in text
    assert "TOP-SECRET-ITEM" not in text
    assert '"list_id"' not in text


def test_the_definition_never_reveals_how_to_reach_the_database(
    client, people, shared, sqlite_connection, target_sqlite
):
    _, charts, _ = shared
    _approved(client, charts[0], people["alice"])
    response = _get(client, people["bob"], charts[0]["id"])
    text = response.text
    # The connection is a name and nothing else.
    assert sqlite_connection["id"] not in text
    assert target_sqlite not in text
    assert "sqlite_path" not in text
    for forbidden in ("connection_id", "host", "port", "database", "username", "password", "ssl"):
        assert f'"{forbidden}"' not in text, forbidden
    assert "alice@example.com" not in text


def test_a_private_chart_is_invisible_to_a_viewer(client, people, shared):
    _, charts, _ = shared
    assert _get(client, people["bob"], charts[1]["id"]).status_code == 404


def test_the_querys_other_published_state_does_not_leak_a_private_sibling(client, people, shared):
    """Publishing one chart shares one chart: the other is still private."""
    _, charts, _ = shared
    _approved(client, charts[0], people["alice"])
    assert _get(client, people["bob"], charts[1]["id"]).status_code == 404
    assert "Private one" not in _get(client, people["bob"], charts[0]["id"]).text


def test_a_pending_chart_is_invisible_to_a_viewer_but_not_to_its_author_or_an_admin(client, people, shared):
    _, charts, _ = shared
    client.post(f"/queries/charts/{charts[0]['id']}/publish", headers=people["alice"])

    assert _get(client, people["bob"], charts[0]["id"]).status_code == 404
    for who in ("alice", "boss"):
        response = _get(client, people[who], charts[0]["id"])
        assert response.status_code == 200, who
        assert response.json()["read_only"] is False, who
        assert response.json()["chart"]["publish_status"] == "pending"


def test_the_author_and_an_admin_get_it_in_every_state_and_may_edit(client, people, shared):
    _, charts, _ = shared
    for who in ("alice", "boss"):
        private = _get(client, people[who], charts[1]["id"])
        assert private.status_code == 200
        assert private.json()["read_only"] is False
    _approved(client, charts[0], people["alice"])
    for who in ("alice", "boss"):
        assert _get(client, people[who], charts[0]["id"]).json()["read_only"] is False


def test_the_definition_stops_when_the_chart_is_unpublished(client, people, shared):
    _, charts, _ = shared
    _approved(client, charts[0], people["alice"])
    assert _get(client, people["bob"], charts[0]["id"]).status_code == 200
    client.post(f"/queries/charts/{charts[0]['id']}/unpublish", headers=people["alice"])
    assert _get(client, people["bob"], charts[0]["id"]).status_code == 404


def test_an_unknown_chart_is_a_404(client, people, app_db):
    assert _get(client, people["bob"], "no-such-chart").status_code == 404


@pytest.mark.parametrize("method", ["put", "post", "patch", "delete"])
def test_there_is_nothing_to_write_to(client, people, shared, method):
    _, charts, _ = shared
    _approved(client, charts[0], people["alice"])
    response = getattr(client, method)(
        f"/queries/charts/{charts[0]['id']}/definition", headers=people["bob"],
        **({"json": {"sql_text": "SELECT 1"}} if method != "delete" else {}),
    )
    assert response.status_code == 405


def test_reading_the_definition_does_not_let_a_viewer_edit_the_query(client, people, shared):
    query, charts, _ = shared
    _approved(client, charts[0], people["alice"])
    _get(client, people["bob"], charts[0]["id"])

    assert client.put(f"/queries/{query['id']}", headers=people["bob"], json={"sql_text": "SELECT 1 AS n"}).status_code == 404
    assert client.get(f"/queries/{query['id']}", headers=people["bob"]).status_code == 404
    assert client.get(f"/queries/{query['id']}/flag-rules", headers=people["bob"]).status_code == 404
    assert client.put(f"/queries/{query['id']}/charts", headers=people["bob"], json={"charts": []}).status_code == 404


def test_the_values_shown_are_the_effective_ones(client, people, sqlite_connection):
    """A query that left them unset still has a limit and an interval it really runs with."""
    query = client.post(
        f"/connections/{sqlite_connection['id']}/queries", headers=people["alice"],
        json={"name": "Plain", "sql_text": SQL},
    ).json()
    chart = client.put(
        f"/queries/{query['id']}/charts", headers=people["alice"],
        json={"charts": [{"name": "Plain chart", "chart_type": "table"}]},
    ).json()["charts"][0]
    _approved(client, chart, people["alice"])

    body = _get(client, people["bob"], chart["id"]).json()["query"]

    assert isinstance(body["row_limit"], int) and body["row_limit"] > 0
    assert isinstance(body["poll_interval_ms"], int) and body["poll_interval_ms"] > 0
    assert body["description"] in (None, "")
