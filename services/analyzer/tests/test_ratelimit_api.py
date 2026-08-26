"""Per-client budgets.

Authentication answers "who are you"; this answers "how much of the target
database can you cost us regardless". A signed-in analyst's session is not a
budget - every execution endpoint still runs caller-supplied SQL against a
customer's production database, and a compromised or merely careless
authenticated caller can hammer it exactly as hard as an anonymous one could
before this existed.
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


def test_general_requests_are_limited(admin_client, monkeypatch):
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    # admin_client's own setup logs in, which is itself a general-bucket
    # request (POST /auth/login is not exempt from rate limiting - it should
    # not be, or the limiter would have a hole an attacker could log in
    # through). That spends one unit of budget before this test's own count
    # starts, so the counter is zeroed here to test what the assertions below
    # actually mean to test: five requests against a budget of three.
    ratelimit.reset()

    codes = [admin_client.get("/connections").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert codes[4] == 429


def test_a_limited_response_carries_the_contract_envelope(admin_client, monkeypatch):
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "1")
    get_settings.cache_clear()

    admin_client.get("/connections")
    response = admin_client.get("/connections")

    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == "RATE_LIMITED"
    assert body["detail"]["limit_per_minute"] == 1
    assert "Retry-After" in response.headers


def test_execution_endpoints_use_their_own_larger_budget(admin_client, monkeypatch):
    """A dashboard poll must not be throttled by ordinary CRUD traffic."""
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("FAE_RATE_LIMIT_EXECUTION_PER_MINUTE", "50")
    get_settings.cache_clear()

    admin_client.get("/connections")
    assert admin_client.get("/connections").status_code == 429

    # The execution bucket is untouched by the exhausted general bucket.
    polled = admin_client.get("/queries/does-not-exist/poll")
    assert polled.status_code == 404


def test_zero_disables_the_limit(admin_client, monkeypatch):
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "0")
    get_settings.cache_clear()
    for _ in range(20):
        assert admin_client.get("/connections").status_code == 200


def test_clients_have_separate_budgets(admin_client, monkeypatch):
    """One noisy caller must not lock everyone else out."""
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()

    for _ in range(3):
        admin_client.get("/connections", headers={"X-Forwarded-For": "10.0.0.1"})
    assert (
        admin_client.get("/connections", headers={"X-Forwarded-For": "10.0.0.1"}).status_code
        == 429
    )
    assert (
        admin_client.get("/connections", headers={"X-Forwarded-For": "10.0.0.2"}).status_code
        == 200
    )


def test_oversized_body_is_refused_before_it_is_read(admin_client, monkeypatch):
    monkeypatch.setenv("FAE_MAX_REQUEST_BYTES", "500")
    get_settings.cache_clear()

    response = admin_client.post(
        "/connections",
        json={"name": "x" * 2000, "db_type": "sqlite", "sqlite_path": "/tmp/x.db"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["max_request_bytes"] == 500
