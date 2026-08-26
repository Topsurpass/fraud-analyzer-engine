"""The loop that runs queries with nobody watching.

This is the only part of the service that touches a customer's production
database unprompted, so the tests are mostly about restraint: what it refuses
to run, how often, and what it does when a target is down.

Driven synchronously through ``run_due_once`` rather than through the asyncio
loop. The cadence is one ``await`` and a timeout; the decisions are all here.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.app_state import get_engine
from app.models import FlaggedRow
from app.services import scheduler

from tests.test_flag_rules_api import make_query, rule


@pytest.fixture(autouse=True)
def _fresh_schedule():
    scheduler.reset()
    yield
    scheduler.reset()


@pytest.fixture
def session():
    with Session(get_engine()) as opened:
        yield opened


@pytest.fixture
def watched(admin_client, sqlite_connection):
    created = make_query(
        admin_client, sqlite_connection["id"], "SELECT day, amount FROM txns", name="watched"
    )
    admin_client.put(
        f"/queries/{created['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    return created


def _stored(session) -> int:
    return session.query(FlaggedRow).count()


def test_a_due_query_runs_and_stores_its_findings(admin_client, watched, session):
    assert scheduler.run_due_once(session) == 1
    assert _stored(session) == 2


def test_a_query_without_rules_is_never_run(admin_client, sqlite_connection, session):
    # It produces nothing to review, so running it on a timer would be load
    # with no output.
    make_query(admin_client, sqlite_connection["id"], "SELECT day FROM txns", name="plain")
    assert scheduler.run_due_once(session) == 0


def test_a_query_is_not_run_again_until_its_interval_elapses(
    admin_client, watched, session
):
    assert scheduler.run_due_once(session) == 1
    assert scheduler.run_due_once(session) == 0


def test_the_interval_has_a_floor(admin_client, sqlite_connection, monkeypatch):
    """A one-second interval must not become a denial of service."""
    monkeypatch.setenv("FAE_SCHEDULER_MIN_INTERVAL_MS", "60000")
    get_settings.cache_clear()

    created = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day FROM txns",
        name="eager",
        poll_interval_ms=1000,
    )
    with Session(get_engine()) as opened:
        from app.models import SavedQuery

        query = opened.get(SavedQuery, created["id"])
        assert scheduler.interval_for(query) == 60_000


def test_a_longer_interval_than_the_floor_is_respected(
    admin_client, sqlite_connection, monkeypatch
):
    monkeypatch.setenv("FAE_SCHEDULER_MIN_INTERVAL_MS", "1000")
    get_settings.cache_clear()

    created = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day FROM txns",
        name="patient",
        poll_interval_ms=900_000,
    )
    with Session(get_engine()) as opened:
        from app.models import SavedQuery

        assert scheduler.interval_for(opened.get(SavedQuery, created["id"])) == 900_000


def test_a_failing_target_backs_off_instead_of_retrying_at_full_rate(
    admin_client, watched, session, monkeypatch
):
    def explode(*_args, **_kwargs):
        raise RuntimeError("target is down")

    monkeypatch.setattr("app.services.query_service.run_saved_query", explode)

    assert scheduler.run_due_once(session) == 0
    first = scheduler._next_due[watched["id"]]

    scheduler._next_due[watched["id"]] = 0  # make it due again
    assert scheduler.run_due_once(session) == 0
    second_factor = scheduler._backoff[watched["id"]]

    assert first > 0
    # Doubling, not a constant retry.
    assert second_factor >= 4


def test_one_broken_target_does_not_stop_the_others(
    admin_client, sqlite_connection, session, monkeypatch
):
    good = make_query(
        admin_client, sqlite_connection["id"], "SELECT day, amount FROM txns", name="good"
    )
    admin_client.put(
        f"/queries/{good['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    bad = make_query(
        admin_client, sqlite_connection["id"], "SELECT day, amount FROM txns", name="bad"
    )
    admin_client.put(
        f"/queries/{bad['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )

    real = scheduler.query_service.run_saved_query

    def selective(query, conn):
        if query.id == bad["id"]:
            raise RuntimeError("this one is down")
        return real(query, conn)

    monkeypatch.setattr("app.services.query_service.run_saved_query", selective)

    assert scheduler.run_due_once(session) == 1
    assert _stored(session) == 2


def test_backoff_clears_after_a_success(admin_client, watched, session, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("app.services.query_service.run_saved_query", explode)
    scheduler.run_due_once(session)
    assert watched["id"] in scheduler._backoff

    monkeypatch.undo()
    scheduler._next_due[watched["id"]] = 0
    assert scheduler.run_due_once(session) == 1
    assert watched["id"] not in scheduler._backoff


def test_backoff_is_capped(admin_client, watched, session, monkeypatch):
    monkeypatch.setenv("FAE_SCHEDULER_MAX_BACKOFF_MS", "5000")
    get_settings.cache_clear()

    def explode(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("app.services.query_service.run_saved_query", explode)
    for _ in range(8):
        scheduler._next_due[watched["id"]] = 0
        scheduler.run_due_once(session)

    # However long it has been failing, it still checks back within the cap.
    assert scheduler._next_due[watched["id"]] <= scheduler._now_ms() + 5000


def test_a_dismissed_row_is_not_re_flagged_by_a_scheduled_run(
    admin_client, watched, session
):
    """The scheduler is exactly what would refill a queue somebody cleared."""
    scheduler.run_due_once(session)
    stored = session.query(FlaggedRow).all()
    victim = stored[0].row_fingerprint
    admin_client.post(
        f"/queries/{watched['id']}/flag-dismissals", json={"fingerprints": [victim]}
    )

    scheduler.reset()
    scheduler.run_due_once(session)
    session.expire_all()
    assert victim not in [row.row_fingerprint for row in session.query(FlaggedRow).all()]


def test_reset_forgets_every_schedule(admin_client, watched, session):
    scheduler.run_due_once(session)
    assert scheduler.run_due_once(session) == 0
    scheduler.reset()
    assert scheduler.run_due_once(session) == 1
