"""End-to-end proof that the credential-dump path is closed.

This is the exact sequence the audit demonstrated against the running service:
register a sqlite connection pointing at the app-state database, then select
the encrypted passwords straight out of it through the public query endpoint.
"""

from __future__ import annotations

from app.config import get_settings


def test_cannot_register_a_connection_pointing_at_the_app_state_database(
    client, tmp_path
):
    app_db_path = get_settings().app_db_sqlite_file
    assert app_db_path is not None, "this test needs the sqlite app-state backend"

    response = client.post(
        "/connections",
        json={
            "name": "exfil",
            "db_type": "sqlite",
            "sqlite_path": str(app_db_path),
        },
    )

    # The profile may be stored, but it must never test OK, because the engine
    # refuses to open the file at all.
    assert response.status_code in (201, 400), response.text
    if response.status_code == 201:
        body = response.json()
        assert body["test_ok"] is False
        assert "app-state database" in (body["test_error"] or "")


def test_credentials_cannot_be_read_back_through_a_target_connection(
    client, tmp_path
):
    """The full attack, end to end, must not return a single row."""
    app_db_path = get_settings().app_db_sqlite_file

    # Give the service a real credential to leak.
    client.post(
        "/connections",
        json={
            "name": "victim",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "d",
            "username": "u",
            "password": "super-secret-value",
        },
    )

    created = client.post(
        "/connections",
        json={"name": "exfil", "db_type": "sqlite", "sqlite_path": str(app_db_path)},
    )
    if created.status_code != 201:
        return  # refused outright, nothing further to probe

    connection_id = created.json()["connection"]["id"]
    stolen = client.post(
        f"/connections/{connection_id}/query/preview",
        json={"sql_text": "SELECT name, password_encrypted FROM connections"},
    )

    assert stolen.status_code != 200, (
        f"app-state database was readable through a target connection: "
        f"{stolen.text[:300]}"
    )


def test_tables_of_the_app_state_database_are_not_listable(client):
    app_db_path = get_settings().app_db_sqlite_file
    created = client.post(
        "/connections",
        json={"name": "exfil2", "db_type": "sqlite", "sqlite_path": str(app_db_path)},
    )
    if created.status_code != 201:
        return

    connection_id = created.json()["connection"]["id"]
    listed = client.get(f"/connections/{connection_id}/tables")
    assert listed.status_code != 200, listed.text
