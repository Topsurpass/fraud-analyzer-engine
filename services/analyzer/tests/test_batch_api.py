"""Batch endpoints.

Every chart on a dashboard ran its own request loop. Twelve cards at the
five-second default is twelve requests to paint plus twelve every tick, each
taking a worker thread, an app-state session, and on a cache miss a target
connection out of a pool of ten.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def three_queries(client, sqlite_connection):
    ids = []
    for i in range(3):
        created = client.post(
            f"/connections/{sqlite_connection['id']}/queries",
            json={
                "name": f"q{i}",
                "sql_text": f"SELECT day, {i} AS n FROM txns ORDER BY day",
                "chart_type": "table",
            },
        )
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    return ids


def test_batch_fetch_returns_queries_in_the_order_asked_for(client, three_queries):
    reversed_ids = list(reversed(three_queries))
    response = client.get("/queries", params={"ids": ",".join(reversed_ids)})
    assert response.status_code == 200
    assert [q["id"] for q in response.json()] == reversed_ids


def test_batch_fetch_skips_unknown_ids(client, three_queries):
    """A board that just lost a query should still render what survived."""
    response = client.get(
        "/queries", params={"ids": f"{three_queries[0]},missing,{three_queries[1]}"}
    )
    assert response.status_code == 200
    assert [q["id"] for q in response.json()] == three_queries[:2]


def test_batch_poll_returns_one_result_per_query(client, three_queries):
    response = client.post(
        "/queries/poll",
        json={"queries": [{"query_id": qid} for qid in three_queries]},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 3
    assert {r["query_id"] for r in results} == set(three_queries)
    assert all(r["changed"] is True for r in results)


def test_batch_poll_honours_since_hash_per_query(client, three_queries):
    first = client.post(
        "/queries/poll", json={"queries": [{"query_id": three_queries[0]}]}
    ).json()["results"][0]

    again = client.post(
        "/queries/poll",
        json={
            "queries": [
                {"query_id": three_queries[0], "since_hash": first["data_hash"]}
            ]
        },
    ).json()["results"][0]

    assert again["changed"] is False
    assert again["data_hash"] == first["data_hash"]


def test_one_broken_query_does_not_fail_the_whole_batch(client, three_queries, session):
    """Eleven working cards must still render beside one broken one."""
    from app.models import SavedQuery

    broken = session.get(SavedQuery, three_queries[1])
    broken.sql_text = "SELECT * FROM table_that_does_not_exist"
    session.commit()

    response = client.post(
        "/queries/poll",
        json={"queries": [{"query_id": qid} for qid in three_queries]},
    )
    assert response.status_code == 200

    results = {r["query_id"]: r for r in response.json()["results"]}
    assert results[three_queries[0]]["changed"] is True
    assert results[three_queries[2]]["changed"] is True
    assert results[three_queries[1]]["ok"] is False
    assert results[three_queries[1]]["error_code"]


def test_batch_poll_reports_a_missing_query_without_failing(client, three_queries):
    response = client.post(
        "/queries/poll",
        json={"queries": [{"query_id": three_queries[0]}, {"query_id": "nope"}]},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["changed"] is True
    assert results[1]["error_code"] == "QUERY_NOT_FOUND"


def test_batch_poll_matches_single_poll_semantics(client, three_queries):
    """The two endpoints share one implementation, so they cannot drift."""
    single = client.get(f"/queries/{three_queries[0]}/poll").json()
    batched = client.post(
        "/queries/poll",
        json={"queries": [{"query_id": three_queries[0]}], "force": True},
    ).json()["results"][0]

    assert single["data_hash"] == batched["data_hash"]
    assert single["columns"] == batched["columns"]
    assert single["rows"] == batched["rows"]


def test_batch_poll_rejects_an_empty_or_oversized_request(client):
    assert client.post("/queries/poll", json={"queries": []}).status_code == 422
    too_many = [{"query_id": f"q{i}"} for i in range(101)]
    assert client.post("/queries/poll", json={"queries": too_many}).status_code == 422
