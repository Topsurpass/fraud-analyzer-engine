"""Connection CRUD, immediate testing, credential containment, cascade delete."""

from __future__ import annotations

import json

from app.models import Connection


def test_create_tests_immediately_and_returns_ok(client, target_sqlite):
    r = client.post(
        "/connections",
        json={"name": "c", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["test_ok"] is True
    assert body["connection"]["status"] == "ok"
    assert body["connection"]["last_tested_at"] is not None


def test_create_saves_even_when_the_test_fails(client, tmp_path):
    r = client.post(
        "/connections",
        json={
            "name": "broken",
            "db_type": "sqlite",
            "sqlite_path": str(tmp_path / "missing.db"),
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["test_ok"] is False
    assert body["connection"]["status"] == "failed"
    assert body["test_error"]
    assert body["test_error_code"] == "DB_UNREACHABLE"

    # The profile is still retrievable, so the user can fix it.
    listed = client.get("/connections").json()
    assert [c["name"] for c in listed] == ["broken"]


def test_response_never_contains_credentials(client):
    r = client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "db.internal",
            "database": "fraud",
            "username": "ro_user",
            "password": "s3cret-do-not-leak",
        },
    )
    assert r.status_code == 201
    payload = json.dumps(r.json())
    assert "s3cret-do-not-leak" not in payload
    assert "password" not in payload
    assert "password_encrypted" not in payload

    connection_id = r.json()["connection"]["id"]
    for body in (
        client.get("/connections").text,
        client.get(f"/connections/{connection_id}").text,
    ):
        assert "password" not in body
        assert "s3cret-do-not-leak" not in body


def test_password_is_encrypted_at_rest(client, session):
    client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "h",
            "database": "d",
            "username": "u",
            "password": "plaintext-never",
        },
    )
    stored = session.query(Connection).one()
    assert stored.password_encrypted
    assert "plaintext-never" not in stored.password_encrypted

    from app.security.crypto import decrypt

    assert decrypt(stored.password_encrypted) == "plaintext-never"


def test_sqlite_rejects_host_fields(client, target_sqlite):
    r = client.post(
        "/connections",
        json={
            "name": "c",
            "db_type": "sqlite",
            "sqlite_path": target_sqlite,
            "host": "nope",
        },
    )
    assert r.status_code == 422
    assert r.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


def test_sqlite_requires_a_path(client):
    r = client.post("/connections", json={"name": "c", "db_type": "sqlite"})
    assert r.status_code == 422


def test_postgres_requires_host_database_username(client):
    r = client.post("/connections", json={"name": "c", "db_type": "postgres"})
    assert r.status_code == 422
    assert "host" in r.text


def test_duplicate_name_is_409(client, target_sqlite):
    payload = {"name": "dupe", "db_type": "sqlite", "sqlite_path": target_sqlite}
    assert client.post("/connections", json=payload).status_code == 201
    r = client.post("/connections", json=payload)
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_get_missing_connection_is_404(client):
    r = client.get("/connections/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error_code"] == "CONNECTION_NOT_FOUND"


def test_retest_updates_status(client, sqlite_connection, target_sqlite, tmp_path):
    r = client.post(f"/connections/{sqlite_connection['id']}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["status"] == "ok"


def test_repointing_at_an_unreachable_target_fails_the_test(
    client, sqlite_connection, tmp_path
):
    # Deleting the target file is not enough to break the connection: on POSIX
    # the pooled SQLite handle keeps the inode alive, so the existing handle
    # still reads fine. Repointing the connection is the real failure path,
    # and it disposes the pooled engine before re-testing.
    r = client.put(
        f"/connections/{sqlite_connection['id']}",
        json={"sqlite_path": str(tmp_path / "gone.db")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["test_ok"] is False
    assert r.json()["connection"]["status"] == "failed"
    assert r.json()["test_error_code"] == "DB_UNREACHABLE"


def test_status_stays_failed_on_subsequent_retest(client, sqlite_connection, tmp_path):
    client.put(
        f"/connections/{sqlite_connection['id']}",
        json={"sqlite_path": str(tmp_path / "gone.db")},
    )
    r = client.post(f"/connections/{sqlite_connection['id']}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["status"] == "failed"
    assert r.json()["error_code"] == "DB_UNREACHABLE"


def test_deleted_target_file_does_not_break_an_open_pooled_handle(
    client, sqlite_connection
):
    # Documents the POSIX behaviour above so the previous test's reasoning is
    # pinned rather than folklore.
    import os

    os.remove(client.get(f"/connections/{sqlite_connection['id']}").json()["sqlite_path"])
    assert client.post(f"/connections/{sqlite_connection['id']}/test").json()["ok"] is True


def test_update_changes_fields_and_retests(client, sqlite_connection, tmp_path):
    r = client.put(
        f"/connections/{sqlite_connection['id']}", json={"name": "renamed"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["name"] == "renamed"
    assert r.json()["test_ok"] is True


def test_update_rejects_host_fields_on_sqlite(client, sqlite_connection):
    r = client.put(f"/connections/{sqlite_connection['id']}", json={"host": "nope"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_CONNECTION_CONFIG"


def test_delete_returns_204_and_removes(client, sqlite_connection):
    assert client.delete(f"/connections/{sqlite_connection['id']}").status_code == 204
    assert client.get(f"/connections/{sqlite_connection['id']}").status_code == 404
    assert client.get("/connections").json() == []


def test_delete_missing_is_404(client):
    assert client.delete("/connections/nope").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_error_envelope_shape(client):
    body = client.get("/connections/nope").json()
    assert set(body) == {"error_code", "message", "detail"}
