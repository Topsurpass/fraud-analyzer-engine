"""Connection CRUD, immediate testing, credential containment, cascade delete."""

from __future__ import annotations

import json

from app.models import Connection


def test_create_tests_immediately_and_returns_ok(admin_client, target_sqlite):
    r = admin_client.post(
        "/connections",
        json={"name": "c", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["test_ok"] is True
    assert body["connection"]["status"] == "ok"
    assert body["connection"]["last_tested_at"] is not None


def test_create_saves_even_when_the_test_fails(admin_client, tmp_path):
    r = admin_client.post(
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
    listed = admin_client.get("/connections").json()
    assert [c["name"] for c in listed] == ["broken"]


def test_response_never_contains_credentials(admin_client):
    # 127.0.0.1:1 refuses instantly, so the immediate connection test fails
    # fast instead of waiting out a DNS lookup or a connect timeout.
    r = admin_client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
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
        admin_client.get("/connections").text,
        admin_client.get(f"/connections/{connection_id}").text,
    ):
        assert "password" not in body
        assert "s3cret-do-not-leak" not in body


def test_password_is_encrypted_at_rest(admin_client, session):
    admin_client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
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


def test_sqlite_rejects_host_fields(admin_client, target_sqlite):
    r = admin_client.post(
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


def test_sqlite_requires_a_path(admin_client):
    r = admin_client.post("/connections", json={"name": "c", "db_type": "sqlite"})
    assert r.status_code == 422


def test_postgres_requires_host_database_username(admin_client):
    r = admin_client.post("/connections", json={"name": "c", "db_type": "postgres"})
    assert r.status_code == 422
    assert "host" in r.text


def test_duplicate_name_is_409(admin_client, target_sqlite):
    payload = {"name": "dupe", "db_type": "sqlite", "sqlite_path": target_sqlite}
    assert admin_client.post("/connections", json=payload).status_code == 201
    r = admin_client.post("/connections", json=payload)
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_get_missing_connection_is_404(admin_client):
    r = admin_client.get("/connections/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error_code"] == "CONNECTION_NOT_FOUND"


def test_retest_updates_status(admin_client, sqlite_connection, target_sqlite, tmp_path):
    r = admin_client.post(f"/connections/{sqlite_connection['id']}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["status"] == "ok"


def test_repointing_at_an_unreachable_target_fails_the_test(
    admin_client, sqlite_connection, tmp_path
):
    # Deleting the target file is not enough to break the connection: on POSIX
    # the pooled SQLite handle keeps the inode alive, so the existing handle
    # still reads fine. Repointing the connection is the real failure path,
    # and it disposes the pooled engine before re-testing.
    r = admin_client.put(
        f"/connections/{sqlite_connection['id']}",
        json={"sqlite_path": str(tmp_path / "gone.db")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["test_ok"] is False
    assert r.json()["connection"]["status"] == "failed"
    assert r.json()["test_error_code"] == "DB_UNREACHABLE"


def test_status_stays_failed_on_subsequent_retest(admin_client, sqlite_connection, tmp_path):
    admin_client.put(
        f"/connections/{sqlite_connection['id']}",
        json={"sqlite_path": str(tmp_path / "gone.db")},
    )
    r = admin_client.post(f"/connections/{sqlite_connection['id']}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["status"] == "failed"
    assert r.json()["error_code"] == "DB_UNREACHABLE"


def test_deleted_target_file_does_not_break_an_open_pooled_handle(
    admin_client, sqlite_connection
):
    # Documents the POSIX behaviour above so the previous test's reasoning is
    # pinned rather than folklore.
    import os

    os.remove(admin_client.get(f"/connections/{sqlite_connection['id']}").json()["sqlite_path"])
    assert admin_client.post(f"/connections/{sqlite_connection['id']}/test").json()["ok"] is True


def test_update_changes_fields_and_retests(admin_client, sqlite_connection, tmp_path):
    r = admin_client.put(
        f"/connections/{sqlite_connection['id']}", json={"name": "renamed"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["name"] == "renamed"
    assert r.json()["test_ok"] is True


def test_update_rejects_host_fields_on_sqlite(admin_client, sqlite_connection):
    r = admin_client.put(f"/connections/{sqlite_connection['id']}", json={"host": "nope"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_CONNECTION_CONFIG"


def test_delete_returns_204_and_removes(admin_client, sqlite_connection):
    assert admin_client.delete(f"/connections/{sqlite_connection['id']}").status_code == 204
    assert admin_client.get(f"/connections/{sqlite_connection['id']}").status_code == 404
    assert admin_client.get("/connections").json() == []


def test_delete_missing_is_404(admin_client):
    assert admin_client.delete("/connections/nope").status_code == 404


def test_health(admin_client):
    assert admin_client.get("/health").json() == {"status": "ok"}


def test_error_envelope_shape(admin_client):
    body = admin_client.get("/connections/nope").json()
    assert set(body) == {"error_code", "message", "detail"}


def test_update_password_reencrypts(admin_client, session):
    created = admin_client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "d",
            "username": "u",
            "password": "first-secret",
        },
    ).json()["connection"]

    r = admin_client.put(f"/connections/{created['id']}", json={"password": "second-secret"})
    assert r.status_code == 200
    assert "second-secret" not in r.text

    from app.security.crypto import decrypt

    stored = session.query(Connection).one()
    assert decrypt(stored.password_encrypted) == "second-secret"


def test_update_rejects_sqlite_path_on_a_non_sqlite_connection(admin_client):
    created = admin_client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "d",
            "username": "u",
        },
    ).json()["connection"]
    r = admin_client.put(f"/connections/{created['id']}", json={"sqlite_path": "/tmp/x.db"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_CONNECTION_CONFIG"


def test_renaming_a_connection_onto_an_existing_name_is_409(admin_client, target_sqlite):
    admin_client.post(
        "/connections",
        json={"name": "first", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )
    second = admin_client.post(
        "/connections",
        json={"name": "second", "db_type": "sqlite", "sqlite_path": target_sqlite},
    ).json()["connection"]
    r = admin_client.put(f"/connections/{second['id']}", json={"name": "first"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "DUPLICATE_NAME"


def test_an_unexpected_probe_error_is_caught_not_crashed(admin_client, target_sqlite, monkeypatch):
    """A probe raising something outside the taxonomy must still be recorded."""
    from app.db import target_registry

    def _explode(*_args, **_kwargs):
        raise ValueError("driver did something unexpected")

    monkeypatch.setattr(target_registry, "probe", _explode)
    r = admin_client.post(
        "/connections",
        json={"name": "odd", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )
    assert r.status_code == 201
    assert r.json()["test_ok"] is False
    assert r.json()["connection"]["status"] == "failed"
    assert r.json()["test_error_code"] == "QUERY_EXECUTION_ERROR"


def test_sqlite_path_on_a_postgres_create_is_rejected(admin_client):
    r = admin_client.post(
        "/connections",
        json={
            "name": "pg",
            "db_type": "postgres",
            "host": "h",
            "database": "d",
            "username": "u",
            "sqlite_path": "/tmp/x.db",
        },
    )
    assert r.status_code == 422
    assert "sqlite_path" in r.text


# ---------------------------------------------------------------------------
# TLS mode
# ---------------------------------------------------------------------------


def test_a_new_connection_defaults_to_require(admin_client):
    r = admin_client.post(
        "/connections",
        json={
            "name": "pg-tls-default",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "fraud",
            "username": "ro_user",
        },
    )
    assert r.status_code == 201
    # Not libpq's "prefer": omitting the mode must not mean "accept plaintext".
    assert r.json()["connection"]["ssl_mode"] == "require"


def test_the_mode_round_trips(admin_client):
    created = admin_client.post(
        "/connections",
        json={
            "name": "pg-verify",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "fraud",
            "username": "ro_user",
            "ssl_mode": "verify-full",
            "ssl_root_cert": "/ca/internal.crt",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["connection"]["id"]

    read = admin_client.get(f"/connections/{connection_id}").json()
    assert read["ssl_mode"] == "verify-full"
    assert read["ssl_root_cert"] == "/ca/internal.crt"


def test_a_certificate_is_rejected_under_a_mode_that_never_reads_it(admin_client):
    r = admin_client.post(
        "/connections",
        json={
            "name": "pg-pointless-cert",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "fraud",
            "username": "ro_user",
            "ssl_mode": "require",
            "ssl_root_cert": "/ca/internal.crt",
        },
    )
    assert r.status_code == 422


def test_relaxing_the_mode_cannot_orphan_a_stored_certificate(admin_client):
    created = admin_client.post(
        "/connections",
        json={
            "name": "pg-relax",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "fraud",
            "username": "ro_user",
            "ssl_mode": "verify-full",
            "ssl_root_cert": "/ca/internal.crt",
        },
    ).json()["connection"]["id"]

    # The payload alone looks harmless; only the merged state shows the
    # certificate left behind with nothing reading it.
    r = admin_client.put(f"/connections/{created}", json={"ssl_mode": "require"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_CONNECTION_CONFIG"


def test_the_mode_can_be_lowered_together_with_the_certificate(admin_client):
    created = admin_client.post(
        "/connections",
        json={
            "name": "pg-lower",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "fraud",
            "username": "ro_user",
            "ssl_mode": "verify-full",
            "ssl_root_cert": "/ca/internal.crt",
        },
    ).json()["connection"]["id"]

    r = admin_client.put(
        f"/connections/{created}",
        json={"ssl_mode": "require", "ssl_root_cert": None},
    )
    assert r.status_code == 200
    assert r.json()["connection"]["ssl_mode"] == "require"


def test_an_unknown_mode_is_refused(admin_client):
    r = admin_client.post(
        "/connections",
        json={
            "name": "pg-bogus",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "fraud",
            "username": "ro_user",
            "ssl_mode": "sort-of",
        },
    )
    assert r.status_code == 422
