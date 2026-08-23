"""The generated schema must describe what the service actually returns.

Every operation used to declare only 200 and a 422 shaped as
{"detail": [ValidationError]} -- FastAPI's default, and not what any handler
emits. A client generated from that got the wrong type for every 4xx and 5xx,
which is why the frontend hand-declares its own ApiErrorBody instead of using
the generated one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import HTTP_STATUS_BY_CODE, ErrorCode
from app.main import app

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "openapi.json"


@pytest.fixture(scope="module")
def schema():
    app.openapi_schema = None
    generated = app.openapi()
    app.openapi_schema = None
    return generated


def test_error_schema_is_declared(schema):
    assert "ApiError" in schema["components"]["schemas"]
    properties = schema["components"]["schemas"]["ApiError"]["properties"]
    assert set(properties) == {"error_code", "message", "detail"}


def test_every_error_code_appears_in_the_schema_enum(schema):
    declared = set(schema["components"]["schemas"]["ApiError"]["properties"]
                   ["error_code"]["enum"])
    assert declared == {code.value for code in ErrorCode}


def test_every_operation_documents_the_error_envelope(schema):
    expected = {str(status) for status in HTTP_STATUS_BY_CODE.values()}
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            missing = expected - set(operation["responses"])
            assert not missing, f"{method.upper()} {path} missing {sorted(missing)}"


def test_error_responses_point_at_the_error_schema_not_the_fastapi_default(schema):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            body = operation["responses"]["422"]["content"]["application/json"]
            assert body["schema"] == {"$ref": "#/components/schemas/ApiError"}, (
                f"{method.upper()} {path} still declares the FastAPI default"
            )


def test_committed_contract_matches_the_running_app(schema):
    """contracts/openapi.json is the frozen artifact clients generate from."""
    committed = json.loads(CONTRACT.read_text())
    if committed != schema:
        report = []
        for key in set(committed) | set(schema):
            if committed.get(key) != schema.get(key):
                if key == "paths":
                    for path in set(committed["paths"]) | set(schema["paths"]):
                        c = committed["paths"].get(path)
                        g = schema["paths"].get(path)
                        if c != g:
                            for method in set(c or {}) | set(g or {}):
                                if (c or {}).get(method) != (g or {}).get(method):
                                    cm = (c or {}).get(method) or {}
                                    gm = (g or {}).get(method) or {}
                                    for k in set(cm) | set(gm):
                                        if cm.get(k) != gm.get(k):
                                            report.append(
                                                f"{method.upper()} {path} [{k}]\n"
                                                f"  committed: {json.dumps(cm.get(k))[:200]}\n"
                                                f"  generated: {json.dumps(gm.get(k))[:200]}"
                                            )
                else:
                    report.append(f"top-level key {key!r} differs")
        raise AssertionError(
            "contracts/openapi.json is stale; regenerate with "
            "scripts/export_openapi.py\n" + "\n".join(report[:10])
        )


def test_batch_endpoints_are_published(schema):
    assert "/queries/poll" in schema["paths"]
    assert "post" in schema["paths"]["/queries/poll"]
    assert "/ready" in schema["paths"]
