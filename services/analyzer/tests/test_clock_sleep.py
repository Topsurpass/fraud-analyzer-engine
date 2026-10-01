"""A paused machine must not make an old result look young.

The bug, from a real execution log: an hourly query ran at 01:45, the laptop
slept from about 03:30 to 09:00, and the 09:02 poll was answered from cache in
45 ms with no run, although the result was six hours old and the cache's limit
is one hour plus a five-minute grace. Docker Desktop pauses its VM while the
host sleeps, and the VM's ``time.monotonic()`` stops with it; the cache measured
age on that clock alone and believed about 47 minutes had passed.

A sleep is simulated here by moving the wall clock forward and leaving the
monotonic clock exactly where it is, which is what the engine saw. Each test
fails if the code goes back to trusting monotonic time alone.
"""

from __future__ import annotations

import time

import pytest

from app import clock
from app.features.queries import refresher, result_cache, scheduler
from tests.test_flag_rules_api import make_query
from tests.test_stale_while_revalidate import _wait_for_refresh

HOUR = 3600.0


@pytest.fixture
def sleep_for(monkeypatch):
    """``sleep_for(s)``: the machine slept for ``s`` seconds. Wall moves, monotonic does not."""
    real_time = time.time
    offset = {"s": 0.0}
    monkeypatch.setattr(time, "time", lambda: real_time() + offset["s"])

    def advance(seconds: float) -> None:
        offset["s"] += seconds

    return advance


@pytest.fixture
def query(admin_client, sqlite_connection):
    return make_query(
        admin_client, sqlite_connection["id"], "SELECT day, amount FROM txns", name="watched"
    )


def _runs(admin_client, query_id: str) -> int:
    return len(admin_client.get(f"/queries/{query_id}/logs").json())


# --- the clock itself -------------------------------------------------------


def test_elapsed_is_the_larger_of_the_two_clocks():
    mono, wall = time.monotonic(), time.time()
    # Monotonic frozen (started "now"), wall says an hour went by.
    assert clock.elapsed_ms(mono, wall - HOUR) >= HOUR * 1000 - 50
    # Wall stepped back, monotonic says an hour went by.
    assert clock.elapsed_ms(mono - HOUR, wall + HOUR) >= HOUR * 1000 - 50


def test_elapsed_is_never_negative():
    assert clock.elapsed_ms(time.monotonic() + 100, time.time() + 100) == 0.0


# --- the result cache -------------------------------------------------------


def test_a_result_is_not_fresh_after_a_sleep_longer_than_its_ttl(sleep_for):
    result_cache.set("q", "h", {"data_hash": "h"}, ttl_ms=int(HOUR * 1000))
    assert result_cache.get("q") is not None
    sleep_for(HOUR + 60)
    assert result_cache.get("q") is None


def test_a_result_is_dropped_after_a_sleep_past_the_grace_window(sleep_for):
    result_cache.set("q", "h", {"data_hash": "h"}, ttl_ms=int(HOUR * 1000))
    sleep_for(6 * HOUR)
    # Six hours is not "at most one interval old"; there is nothing to serve.
    assert result_cache.get_stale("q") is None


def test_a_result_inside_the_grace_window_is_still_served_as_stale(sleep_for):
    result_cache.set("q", "h", {"data_hash": "h"}, ttl_ms=int(HOUR * 1000))
    sleep_for(HOUR + 30)
    assert result_cache.get("q") is None
    assert result_cache.get_stale("q") is not None


def test_a_wall_clock_stepped_backwards_does_not_make_a_result_younger(sleep_for):
    result_cache.set("q", "h", {"data_hash": "h"}, ttl_ms=1000)
    entry = result_cache.get("q")
    assert entry is not None
    entry.stored_at -= 5  # five real seconds passed, by the monotonic clock
    sleep_for(-HOUR)  # and the wall clock was stepped back an hour
    assert result_cache.get("q") is None


def test_a_patched_entry_keeps_its_wall_age(sleep_for):
    """Editing a chart must not reset the clock on either reading."""
    result_cache.set("q", "h", {"data_hash": "h", "columns": ["a"], "charts": []}, ttl_ms=1000)
    before = result_cache.get_stale("q")
    assert before is not None
    stored_wall = before.stored_wall
    result_cache.patch_charts("q", lambda columns: [])
    assert result_cache.get_stale("q").stored_wall == stored_wall


# --- through the API --------------------------------------------------------


def test_a_poll_after_a_long_sleep_runs_the_query(admin_client, query, sleep_for):
    admin_client.post(f"/queries/{query['id']}/run")
    runs = _runs(admin_client, query["id"])

    sleep_for(6 * HOUR)
    body = admin_client.get(f"/queries/{query['id']}/poll").json()

    assert body["from_cache"] is False
    assert _runs(admin_client, query["id"]) == runs + 1


def test_a_poll_just_past_the_interval_after_a_sleep_refreshes_behind_the_answer(
    admin_client, query, sleep_for
):
    admin_client.post(f"/queries/{query['id']}/run")
    runs = _runs(admin_client, query["id"])
    interval = admin_client.get(f"/queries/{query['id']}/poll").json()["poll_interval_ms"]

    sleep_for(interval / 1000 + 1)
    body = admin_client.get(f"/queries/{query['id']}/poll").json()
    _wait_for_refresh()

    assert body["from_cache"] is True  # the last answer, immediately
    assert _runs(admin_client, query["id"]) == runs + 1  # and the new one behind it


# --- the failure cooldown ---------------------------------------------------


def test_a_failure_cooldown_does_not_stretch_over_a_sleep(sleep_for):
    refresher.reset()
    refresher._hold_off("q", int(HOUR * 1000))
    with refresher._lock:
        assert refresher._cooling_down("q") is True
    sleep_for(HOUR + 1)
    with refresher._lock:
        assert refresher._cooling_down("q") is False
    refresher.reset()


def test_a_failure_cooldown_still_holds_inside_its_interval(sleep_for):
    refresher.reset()
    refresher._hold_off("q", int(HOUR * 1000))
    sleep_for(HOUR / 2)
    with refresher._lock:
        assert refresher._cooling_down("q") is True
    refresher.reset()


# --- the scheduler ----------------------------------------------------------


def test_a_scheduled_query_is_due_after_a_sleep(sleep_for):
    scheduler.reset()
    scheduler._set_due_in("q", HOUR * 1000)
    assert scheduler._is_due("q") is False
    sleep_for(HOUR + 1)
    assert scheduler._is_due("q") is True
    scheduler.reset()


def test_a_scheduled_query_is_not_due_early(sleep_for):
    scheduler.reset()
    scheduler._set_due_in("q", HOUR * 1000)
    sleep_for(HOUR / 2)
    assert scheduler._is_due("q") is False
    scheduler.reset()
