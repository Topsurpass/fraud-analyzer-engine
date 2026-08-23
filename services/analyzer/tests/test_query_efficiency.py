"""Regression guards for query-count and copy behaviour.

These assert *mechanism*, not wall-clock: a timing assertion on a laptop is
noise, but "this endpoint must issue exactly two SQL statements no matter how
many rows exist" either holds or does not. Local SQLite makes an N+1 invisible
in wall-clock terms while it still costs a round trip each against a managed
Postgres, which is exactly why counting is the right assertion.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.db.app_state import get_engine


@contextmanager
def count_statements():
    """Record every SQL statement the app-state engine executes."""
    statements: list[str] = []

    def before(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


_names = itertools.count()


def _make_dashboards(client, sqlite_connection, count):
    """Create `count` boards, each holding one freshly named saved query."""
    created = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        json={
            "name": f"q-{next(_names)}",
            "sql_text": "SELECT day FROM txns",
            "chart_type": "table",
        },
    )
    assert created.status_code == 201, created.text
    query = created.json()
    for _ in range(count):
        response = client.post(
            "/dashboards",
            json={"name": f"board-{next(_names)}", "query_ids": [query["id"]]},
        )
        assert response.status_code == 201, response.text
    return query


def test_listing_dashboards_does_not_scale_statements_with_board_count(
    client, sqlite_connection
):
    """The N+1 this replaced: five boards measured six statements.

    query_ids walks Dashboard.items, so a lazy load emitted one SELECT per
    board on a view the frontend loads on every page.
    """
    _make_dashboards(client, sqlite_connection, 5)

    with count_statements() as statements:
        response = client.get("/dashboards")
    assert response.status_code == 200
    assert len(response.json()) == 5

    selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
    assert len(selects) == 2, (
        f"expected 2 statements regardless of board count, got "
        f"{len(selects)}:\n" + "\n".join(selects)
    )


def test_dashboard_statement_count_is_flat_as_boards_grow(client, sqlite_connection):
    """Two boards and ten boards must cost the same number of round trips."""
    _make_dashboards(client, sqlite_connection, 2)
    with count_statements() as few:
        client.get("/dashboards")

    _make_dashboards(client, sqlite_connection, 8)
    with count_statements() as many:
        client.get("/dashboards")

    count_few = len([s for s in few if s.strip().upper().startswith("SELECT")])
    count_many = len([s for s in many if s.strip().upper().startswith("SELECT")])
    assert count_few == count_many, f"{count_few} -> {count_many} as boards grew"


def test_batch_query_fetch_is_one_statement(client, sqlite_connection):
    """A twelve-card board was thirteen round trips before it could paint."""
    ids = []
    for i in range(6):
        created = client.post(
            f"/connections/{sqlite_connection['id']}/queries",
            json={
                "name": f"q{i}",
                "sql_text": "SELECT day FROM txns",
                "chart_type": "table",
            },
        )
        ids.append(created.json()["id"])

    with count_statements() as statements:
        response = client.get("/queries", params={"ids": ",".join(ids)})

    assert response.status_code == 200
    assert len(response.json()) == 6
    selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
    assert len(selects) == 1, "\n".join(selects)


def test_run_payload_does_not_copy_the_rows():
    """as_dict references the rows; dataclasses.asdict deep-copied every cell.

    For a 10,000 x 5 result that was 50,000 deepcopy calls and a full second
    copy of the payload, built and then immediately discarded.
    """
    from datetime import datetime, timezone

    from app.services.query_service import RunPayload

    rows = [[1, "a"], [2, "b"]]
    payload = RunPayload(
        query_id="q",
        executed_at=datetime.now(timezone.utc),
        duration_ms=1,
        row_count=2,
        truncated=False,
        data_hash="sha256:x",
        columns=["n", "s"],
        rows=rows,
        chart={},
    )

    body = payload.as_dict(5000)
    assert body["rows"] is rows, "rows were copied instead of referenced"
    assert body["poll_interval_ms"] == 5000


def test_column_listing_does_not_scan_the_catalog_on_the_happy_path(
    client, sqlite_connection, monkeypatch
):
    """The old code listed every table and view just to build a 404.

    On a warehouse with thousands of tables that listing is a catalog scan,
    and it ran on every column expansion in the schema browser.
    """
    from sqlalchemy.engine import reflection

    calls: list[str] = []
    original = reflection.Inspector.get_table_names

    def spy(self, *args, **kwargs):
        calls.append("get_table_names")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(reflection.Inspector, "get_table_names", spy)

    response = client.get(f"/connections/{sqlite_connection['id']}/tables/txns/columns")
    assert response.status_code == 200
    assert calls == [], "the catalog was listed on a successful column lookup"


def test_unknown_table_still_returns_a_precise_404(client, sqlite_connection):
    """The listing is still consulted, but only where its cost buys something."""
    response = client.get(
        f"/connections/{sqlite_connection['id']}/tables/nope/columns"
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "TABLE_NOT_FOUND"
