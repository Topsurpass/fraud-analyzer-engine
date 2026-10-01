"""Serving the last known chart instead of blocking on the database.

A poll used to run the query inline whenever the cache had expired. With the
default five-second TTL and a query taking two seconds against the target,
roughly half of every card's polls blocked - and the card showed nothing while
they did, because there was no answer to show yet.

These tests are about the trade that fixes it: the reader sees data that is at
most one interval old, immediately, and the fresh copy arrives behind it.
"""

from __future__ import annotations

import time

import pytest

from app.features.queries import refresher, result_cache
from tests.test_flag_rules_api import make_query


@pytest.fixture
def query(admin_client, sqlite_connection):
    return make_query(
        admin_client, sqlite_connection["id"], "SELECT day, amount FROM txns", name="watched"
    )


def _wait_for_refresh(timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while refresher.in_flight_count() > 0 and time.monotonic() < deadline:
        time.sleep(0.02)


def _expire(query_id: str) -> None:
    """Age the entry past its TTL without waiting for real time to pass."""
    entry = result_cache.get_stale(query_id)
    assert entry is not None
    entry.stored_at -= (entry.ttl_ms / 1000) + 1


def test_a_poll_past_the_ttl_still_answers_with_data(admin_client, query):
    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])

    body = admin_client.get(f"/queries/{query['id']}/poll").json()

    # The whole point: rows now, not after a round trip to the target.
    assert body["row_count"] > 0
    assert body["from_cache"] is True
    _wait_for_refresh()


def test_the_stale_answer_triggers_a_refresh(admin_client, query):
    admin_client.post(f"/queries/{query['id']}/run")
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())
    _expire(query["id"])

    admin_client.get(f"/queries/{query['id']}/poll")
    _wait_for_refresh()

    assert len(admin_client.get(f"/queries/{query['id']}/logs").json()) == before + 1


def test_many_polls_of_one_stale_query_cause_one_execution(admin_client, query):
    """Twenty cards watching one query must not become twenty executions.

    Expiry is exactly when a stampede is worst: every card asks at the same
    moment, and the database is already the slow part.
    """
    admin_client.post(f"/queries/{query['id']}/run")
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())
    _expire(query["id"])

    for _ in range(10):
        admin_client.get(f"/queries/{query['id']}/poll")
    _wait_for_refresh()

    assert len(admin_client.get(f"/queries/{query['id']}/logs").json()) == before + 1


def test_the_refresh_actually_lands_in_the_cache(admin_client, query):
    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])
    admin_client.get(f"/queries/{query['id']}/poll")
    _wait_for_refresh()

    # Fresh again, so the next poll is a plain cache hit.
    assert result_cache.get(query["id"]) is not None


def test_a_query_nobody_has_run_still_blocks_once(admin_client, query):
    # There is nothing to serve, so this one has to wait - and only this one.
    body = admin_client.get(f"/queries/{query['id']}/poll").json()
    assert body["row_count"] > 0
    assert body["from_cache"] is False


def test_a_forced_refresh_never_serves_stale(admin_client, query):
    # Someone pressing Refresh is asking for a fresh read; handing them the old
    # answer would make the button look broken.
    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])
    body = admin_client.get(f"/queries/{query['id']}/poll", params={"force": True}).json()
    assert body["from_cache"] is False


def test_an_entry_past_the_grace_window_is_not_served(admin_client, query, monkeypatch):
    """Stale beats nothing, but "an hour ago" does not answer "what now"."""
    monkeypatch.setenv("FAE_CACHE_STALE_GRACE_MS", "1")
    from app.config import get_settings

    get_settings.cache_clear()

    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])
    assert result_cache.get_stale(query["id"]) is None


def test_a_disconnected_connection_is_not_refreshed_behind_your_back(
    admin_client, sqlite_connection, query
):
    """Disconnected means disconnected, including for background work."""
    admin_client.post(f"/queries/{query['id']}/run")
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())
    admin_client.post(f"/connections/{sqlite_connection['id']}/disconnect")
    _expire(query["id"])

    refresher.request_refresh(query["id"], None)
    _wait_for_refresh()

    assert len(admin_client.get(f"/queries/{query['id']}/logs").json()) == before


def test_a_failing_refresh_does_not_take_the_request_with_it(admin_client, query, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("target is down")

    monkeypatch.setattr("app.features.queries.execution.run_saved_query", explode)

    admin_client.post(f"/queries/{query['id']}/run") if False else None
    refresher.request_refresh(query["id"], None)
    _wait_for_refresh()
    # No exception escaped, and the tracker is clean for the next attempt.
    assert refresher.in_flight_count() == 0


def test_the_refresh_a_poll_starts_names_the_person_who_polled(admin_client, query):
    """The refresh is not anonymous work.

    ``request_refresh`` took only a ``query_id``, so a background run started
    by a named analyst's poll landed in the execution log with no user - the
    same shape as the scheduler's rows, which genuinely have nobody behind
    them. A poll is a person asking; the run it triggers is attributable to
    that person even though it happens after the response.
    """
    admin_client.post(f"/queries/{query['id']}/run")
    me = admin_client.get("/auth/me").json()["id"]
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())
    _expire(query["id"])

    admin_client.get(f"/queries/{query['id']}/poll")
    _wait_for_refresh()

    logs = admin_client.get(f"/queries/{query['id']}/logs").json()
    assert len(logs) > before
    assert logs[0]["user_id"] == me


def test_a_failing_refresh_still_names_the_person_who_polled(admin_client, query, monkeypatch):
    """A failed background run is still a run against their database, and the
    execution log is where somebody looks to find out why a card is stale."""
    admin_client.post(f"/queries/{query['id']}/run")
    me = admin_client.get("/auth/me").json()["id"]
    _expire(query["id"])

    def explode(*_args, **_kwargs):
        from app.errors import AppError, ErrorCode

        raise AppError(ErrorCode.QUERY_EXECUTION_ERROR, "target is down")

    monkeypatch.setattr("app.features.queries.execution.run_saved_query", explode)
    refresher.request_refresh(query["id"], me)
    _wait_for_refresh()

    logs = admin_client.get(f"/queries/{query['id']}/logs").json()
    assert logs[0]["success"] is False
    assert logs[0]["user_id"] == me


class _FailingTarget:
    """Stands in for a database that is down, and counts how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError("target is down")


def test_a_failed_refresh_is_not_retried_until_the_interval_has_passed(admin_client, query, monkeypatch):
    """A client polling a stale result must not turn into a storm against a dead database.

    A failed refresh leaves the stale entry in place, so every following poll
    found it stale again and started another attempt.
    """
    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])
    down = _FailingTarget()
    monkeypatch.setattr("app.features.queries.execution.run_saved_query", down)

    assert refresher.request_refresh(query["id"], None) is True
    _wait_for_refresh()
    assert down.calls == 1

    for _ in range(10):
        admin_client.get(f"/queries/{query['id']}/poll")
    _wait_for_refresh()
    assert down.calls == 1, "a failed refresh must not be retried inside its interval"


def test_the_cooldown_lasts_one_poll_interval_and_then_lifts(admin_client, query, monkeypatch):
    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])
    monkeypatch.setattr("app.features.queries.execution.run_saved_query", _FailingTarget())
    refresher.request_refresh(query["id"], None)
    _wait_for_refresh()

    interval_s = 5000 / 1000  # the default poll interval, which this query uses
    remaining = refresher._cooldown_until[query["id"]] - time.monotonic()
    assert 0 < remaining <= interval_s

    refresher._cooldown_until[query["id"]] = time.monotonic() - 1  # the interval has passed
    assert refresher.request_refresh(query["id"], None) is True
    _wait_for_refresh()


def test_a_successful_refresh_clears_the_cooldown(admin_client, query, monkeypatch):
    admin_client.post(f"/queries/{query['id']}/run")
    _expire(query["id"])
    refresher._cooldown_until[query["id"]] = time.monotonic() - 1
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())

    assert refresher.request_refresh(query["id"], None) is True
    _wait_for_refresh()

    assert len(admin_client.get(f"/queries/{query['id']}/logs").json()) == before + 1
    assert query["id"] not in refresher._cooldown_until


def test_the_cooldown_follows_the_query_own_interval(admin_client, sqlite_connection, monkeypatch):
    """An hourly query is left alone for an hour after a failure, not for five seconds."""
    hourly = make_query(
        admin_client, sqlite_connection["id"], "SELECT day FROM txns", name="hourly"
    )
    admin_client.put(f"/queries/{hourly['id']}", json={"poll_interval_ms": 3_600_000})
    admin_client.post(f"/queries/{hourly['id']}/run")
    _expire(hourly["id"])
    monkeypatch.setattr("app.features.queries.execution.run_saved_query", _FailingTarget())

    refresher.request_refresh(hourly["id"], None)
    _wait_for_refresh()

    remaining = refresher._cooldown_until[hourly["id"]] - time.monotonic()
    assert 3590 < remaining <= 3600


def test_a_forced_poll_ignores_the_cooldown(admin_client, query):
    """"Run now" is a person asking, and it does not come through the refresher."""
    admin_client.post(f"/queries/{query['id']}/run")
    refresher._cooldown_until[query["id"]] = time.monotonic() + 3600
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())

    admin_client.get(f"/queries/{query['id']}/poll?force=true")

    assert len(admin_client.get(f"/queries/{query['id']}/logs").json()) == before + 1
