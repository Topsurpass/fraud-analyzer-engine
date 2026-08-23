"""Per-client budgets.

There is no auth, so every execution endpoint runs caller-supplied SQL against
a customer's production database with nothing but this bounding it.
"""

from __future__ import annotations

import pytest

from app import ratelimit
from app.config import get_settings


@pytest.fixture(autouse=True)
def _clean_buckets():
    ratelimit.reset()
    yield
    ratelimit.reset()


def test_general_requests_are_limited(client, monkeypatch):
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()

    codes = [client.get("/connections").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert codes[4] == 429


def test_a_limited_response_carries_the_contract_envelope(client, monkeypatch):
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "1")
    get_settings.cache_clear()

    client.get("/connections")
    response = client.get("/connections")

    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == "RATE_LIMITED"
    assert body["detail"]["limit_per_minute"] == 1
    assert "Retry-After" in response.headers


def test_execution_endpoints_use_their_own_larger_budget(client, monkeypatch):
    """A dashboard poll must not be throttled by ordinary CRUD traffic."""
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("FAE_RATE_LIMIT_EXECUTION_PER_MINUTE", "50")
    get_settings.cache_clear()

    client.get("/connections")
    assert client.get("/connections").status_code == 429

    # The execution bucket is untouched by the exhausted general bucket.
    polled = client.get("/queries/does-not-exist/poll")
    assert polled.status_code == 404


def test_zero_disables_the_limit(client, monkeypatch):
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "0")
    get_settings.cache_clear()
    for _ in range(20):
        assert client.get("/connections").status_code == 200


def test_clients_have_separate_budgets(client, monkeypatch):
    """One noisy caller must not lock everyone else out."""
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()

    for _ in range(3):
        client.get("/connections", headers={"X-Forwarded-For": "10.0.0.1"})
    assert (
        client.get("/connections", headers={"X-Forwarded-For": "10.0.0.1"}).status_code
        == 429
    )
    assert (
        client.get("/connections", headers={"X-Forwarded-For": "10.0.0.2"}).status_code
        == 200
    )


def test_oversized_body_is_refused_before_it_is_read(client, monkeypatch):
    monkeypatch.setenv("FAE_MAX_REQUEST_BYTES", "500")
    get_settings.cache_clear()

    response = client.post(
        "/connections",
        json={"name": "x" * 2000, "db_type": "sqlite", "sqlite_path": "/tmp/x.db"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["max_request_bytes"] == 500
