"""Schema discovery against an arbitrary target schema."""

from __future__ import annotations


def test_lists_tables_and_views(client, sqlite_connection):
    r = client.get(f"/connections/{sqlite_connection['id']}/tables")
    assert r.status_code == 200, r.text
    body = r.json()
    by_name = {t["name"]: t["kind"] for t in body["tables"]}
    assert by_name["txns"] == "table"
    assert by_name["flagged_txns"] == "view"


def test_lists_columns_with_types(client, sqlite_connection):
    r = client.get(f"/connections/{sqlite_connection['id']}/tables/txns/columns")
    assert r.status_code == 200, r.text
    columns = {c["name"]: c for c in r.json()["columns"]}
    assert set(columns) == {"id", "day", "user_id", "amount", "flagged", "comment"}
    assert columns["amount"]["type"].upper().startswith("REAL")
    assert columns["day"]["nullable"] is False
    assert columns["id"]["primary_key"] is True
    assert columns["comment"]["nullable"] is True


def test_columns_of_a_view(client, sqlite_connection):
    r = client.get(f"/connections/{sqlite_connection['id']}/tables/flagged_txns/columns")
    assert r.status_code == 200
    assert {c["name"] for c in r.json()["columns"]} >= {"id", "flagged"}


def test_unknown_table_is_404(client, sqlite_connection):
    r = client.get(f"/connections/{sqlite_connection['id']}/tables/nope/columns")
    assert r.status_code == 404
    assert r.json()["error_code"] == "TABLE_NOT_FOUND"


def test_introspection_on_missing_connection_is_404(client):
    assert client.get("/connections/nope/tables").status_code == 404


def test_introspection_on_broken_connection_is_502(client, tmp_path):
    created = client.post(
        "/connections",
        json={
            "name": "broken",
            "db_type": "sqlite",
            "sqlite_path": str(tmp_path / "gone.db"),
        },
    ).json()["connection"]
    r = client.get(f"/connections/{created['id']}/tables")
    assert r.status_code == 502
    assert r.json()["error_code"] == "DB_UNREACHABLE"
