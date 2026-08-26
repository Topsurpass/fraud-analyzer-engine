"""Every failure the API can produce comes back in one envelope."""

from __future__ import annotations

import pytest
from fastapi import APIRouter

from app.errors import HTTP_STATUS_BY_CODE, ErrorCode

ENVELOPE = {"error_code", "message", "detail"}


def test_every_error_code_has_a_status():
    assert set(HTTP_STATUS_BY_CODE) == set(ErrorCode)


@pytest.mark.parametrize(
    "path,expected_status,expected_code",
    [
        ("/connections/nope", 404, "CONNECTION_NOT_FOUND"),
        ("/queries/nope", 404, "QUERY_NOT_FOUND"),
        ("/connections/nope/tables", 404, "CONNECTION_NOT_FOUND"),
        ("/connections/nope/queries", 404, "CONNECTION_NOT_FOUND"),
    ],
)
def test_not_found_envelope(admin_client, path, expected_status, expected_code):
    r = admin_client.get(path)
    assert r.status_code == expected_status
    assert set(r.json()) == ENVELOPE
    assert r.json()["error_code"] == expected_code


def test_body_validation_failure_uses_the_same_envelope(admin_client):
    r = admin_client.post("/connections", json={"db_type": "sqlite"})
    assert r.status_code == 422
    assert set(r.json()) == ENVELOPE
    assert r.json()["error_code"] == "REQUEST_VALIDATION_ERROR"
    assert r.json()["detail"]["errors"]


def test_unknown_db_type_is_a_validation_error(admin_client):
    r = admin_client.post(
        "/connections", json={"name": "x", "db_type": "oracle", "sqlite_path": "/x"}
    )
    assert r.status_code == 422
    assert r.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


def test_query_param_validation_failure(admin_client):
    r = admin_client.get("/queries/nope/logs", params={"limit": 0})
    assert r.status_code == 422
    assert r.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


def test_validation_detail_is_json_serialisable(admin_client):
    # Pydantic puts non-serialisable objects in ctx; the handler must strip them.
    r = admin_client.post("/connections", json={"name": "x", "db_type": "sqlite"})
    assert r.status_code == 422
    for entry in r.json()["detail"]["errors"]:
        assert set(entry) == {"loc", "msg", "type"}


def test_unhandled_exception_returns_500_without_leaking(admin_client):
    from app.main import app

    boom = APIRouter()

    @boom.get("/_boom")
    def _boom():
        raise RuntimeError("secret internal detail: password=hunter2")

    # Snapshot and restore, rather than filtering by path afterwards. The
    # filter version removed far more than it meant to and left the app
    # without its API routers for every test that ran later.
    original_routes = list(app.routes)
    original_schema = app.openapi_schema
    app.include_router(boom)
    app.openapi_schema = None
    try:
        r = admin_client.get("/_boom")
        assert r.status_code == 500
        assert set(r.json()) == ENVELOPE
        assert r.json()["error_code"] == "INTERNAL_ERROR"
        assert "hunter2" not in r.text
        assert "Traceback" not in r.text
        assert r.json()["detail"] is None
    finally:
        app.routes[:] = original_routes
        app.openapi_schema = original_schema


def test_guard_rejection_names_a_specific_reason_not_a_generic_403(
    admin_client, sqlite_connection
):
    r = admin_client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT 1; DROP TABLE txns"},
    )
    assert r.status_code == 400
    assert r.json()["error_code"] == "MULTIPLE_STATEMENTS"
    assert "multiple statements" in r.json()["message"].lower()


def test_unreachable_target_is_502(admin_client, tmp_path):
    created = admin_client.post(
        "/connections",
        json={"name": "gone", "db_type": "sqlite", "sqlite_path": str(tmp_path / "x.db")},
    ).json()["connection"]
    r = admin_client.get(f"/connections/{created['id']}/tables")
    assert r.status_code == 502
    assert r.json()["error_code"] == "DB_UNREACHABLE"


def test_cors_headers_are_present(admin_client):
    r = admin_client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers["access-control-allow-origin"] == "*"


def test_cors_preflight_allowed(admin_client):
    r = admin_client.options(
        "/connections",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-methods" in r.headers


def test_openapi_document_builds(admin_client):
    r = admin_client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for expected in (
        "/connections",
        "/connections/{connection_id}/test",
        "/connections/{connection_id}/tables",
        "/connections/{connection_id}/tables/{table_name}/columns",
        "/connections/{connection_id}/queries",
        "/connections/{connection_id}/query/preview",
        "/queries/{query_id}",
        "/queries/{query_id}/run",
        "/queries/{query_id}/poll",
    ):
        assert expected in paths, expected


def test_no_schema_in_openapi_exposes_a_password_field(admin_client):
    schemas = admin_client.get("/openapi.json").json()["components"]["schemas"]
    for name, schema in schemas.items():
        if name.startswith("ConnectionRead"):
            assert "password" not in schema.get("properties", {})
            assert "password_encrypted" not in schema.get("properties", {})
