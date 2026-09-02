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


def test_unauthenticated_clients_have_separate_budgets(client, monkeypatch):
    """One noisy caller must not lock everyone else out.

    Unauthenticated, so the address really is the key. This used to run on an
    authenticated client, which no longer demonstrates anything: a request
    carrying a session is keyed on the session, and two addresses sharing one
    token are correctly one caller. The authenticated half of the rule is
    covered by test_two_analysts_behind_one_address_do_not_share_a_budget.
    """
    monkeypatch.setenv("FAE_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()

    for _ in range(3):
        client.get("/connections", headers={"X-Forwarded-For": "10.0.0.1"})
    assert (
        client.get("/connections", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429
    )
    # A different address, still unauthenticated: 401 rather than 429 is the
    # point. It got past the bucket and was turned away by auth instead.
    assert (
        client.get("/connections", headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 401
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


# ---------------------------------------------------------------------------
# Whose budget is it
# ---------------------------------------------------------------------------


def test_two_analysts_behind_one_address_do_not_share_a_budget(client, app_db, monkeypatch):
    """The office-NAT bug.

    A board of twelve cards on the five-second default is 144 polls a minute,
    against an execution bucket of 300. Keyed on the address, the third analyst
    to open a dashboard took the whole office over the limit, and it looked
    like the engine was broken rather than like a quota.
    """
    from app import ratelimit
    from app.models.enums import UserRole
    from tests.test_auth_api import login, make_user

    ratelimit.reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 4, raising=False)

    make_user(email="ada@example.com", role=UserRole.ANALYST)
    make_user(email="kemi@example.com", role=UserRole.ANALYST)
    first = login(client, email="ada@example.com").json()["token"]
    second = login(client, email="kemi@example.com").json()["token"]

    # The first analyst spends their whole budget.
    for _ in range(4):
        client.get("/dashboards", headers={"Authorization": f"Bearer {first}"})
    spent = client.get("/dashboards", headers={"Authorization": f"Bearer {first}"})
    assert spent.status_code == 429

    # The second, from the same address, is unaffected.
    fresh = client.get("/dashboards", headers={"Authorization": f"Bearer {second}"})
    assert fresh.status_code == 200, "one analyst's polling throttled another"


def test_unauthenticated_floods_are_still_bounded_by_address(client, app_db, monkeypatch):
    """A password-guessing flood carries no session, and the address is the
    only identity it has. Keying sign-in attempts on a caller-supplied token
    would let an attacker mint themselves an unlimited budget."""
    from app import ratelimit

    ratelimit.reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3, raising=False)

    codes = [
        client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "wrong"}
        ).status_code
        for _ in range(5)
    ]

    assert 429 in codes, "an unauthenticated flood was not bounded"
