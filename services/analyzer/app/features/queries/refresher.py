"""Refreshing a query's cached result behind the request that asked for it.

A poll used to run the query inline whenever the cache had expired. With the
default five-second TTL and a query taking two seconds in the customer's
database, roughly half of every card's polls blocked - and the card showed
nothing at all while they did, because there was no answer to show yet.

Serving the previous answer immediately and refreshing behind it is what makes
a chart appear as soon as there is anything to draw. The refresh lands in the
cache, the next poll notices the hash moved, and the card redraws. Nothing
waits on the database except the very first load of a query nobody has run.

Two bounds, and both matter because this touches a production database without
anyone asking it to:

* **One refresh per query at a time.** Twenty cards polling one stale query
  must produce one execution, not twenty. Without this, expiry turns into a
  stampede precisely when the database is already slow.
* **A bounded pool.** Refreshes run on a small thread pool rather than a thread
  per request, so a page full of expired cards cannot open a connection per
  card.
* **A cooldown after a failure.** A refresh that fails leaves the stale entry in
  place, so the very next poll would find it stale again and start another
  attempt, and a client polling every few seconds would then retry a database
  that is already down at that rate. After a failure a query is left alone for
  its own poll interval: the same "no more than once per interval" the success
  path keeps. A person can still force a run (``?force=true``, "Run now"), which
  does not come through here.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.app_state import get_engine
from app.errors import AppError
from app.features.connections.models import Connection
from app.features.flag_rules import flagged_rows as flagged_row_service
from app.features.queries import (
    execution as query_service,
)
from app.features.queries import (
    result_cache,
)
from app.features.queries import (
    service as saved_query_service,
)
from app.features.queries.models import SavedQuery

logger = logging.getLogger(__name__)

#: Small on purpose. This exists to keep a page responsive, not to fan out.
_MAX_WORKERS = 4

_pool: ThreadPoolExecutor | None = None
_in_flight: set[str] = set()
#: Per query: the ``time.monotonic()`` before which a failed one is not retried.
_cooldown_until: dict[str, float] = {}
#: The same moment on the wall clock. Both must say "not yet" for a query to be
#: held off, because the monotonic clock stops while the machine sleeps (see
#: ``app/clock.py``) and would otherwise stretch an hour's cooldown over a night.
_cooldown_wall: dict[str, float] = {}
_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, thread_name_prefix="fae-refresh"
            )
        return _pool


def request_refresh(query_id: str, user_id: str | None) -> bool:
    """Ask for a background refresh. Returns whether one was actually started.

    False means one is already running for this query, which is the common case
    on a dashboard: every card watching a stale query asks at once and exactly
    one execution follows.

    ``user_id`` is who caused it - the analyst whose poll found the entry
    stale. It is required rather than defaulted so that a future call site
    cannot quietly file its runs under "nobody"; the scheduler, which really
    does have nobody behind it, does not come through here at all. When
    several people poll the same stale query at once the winner's id is the
    one recorded, because exactly one execution happens and it is theirs: the
    others are served the cached answer and cause no run.
    """
    with _lock:
        if query_id in _in_flight:
            return False
        if _cooling_down(query_id):
            return False
        _in_flight.add(query_id)

    try:
        _get_pool().submit(_refresh, query_id, user_id)
        return True
    except RuntimeError:  # pragma: no cover - pool shut down mid-request
        with _lock:
            _in_flight.discard(query_id)
        return False


def _cooling_down(query_id: str) -> bool:
    """Whether a failed query is still being left alone. Caller holds the lock."""
    until = _cooldown_until.get(query_id)
    if until is None or time.monotonic() >= until:
        return False
    wall_until = _cooldown_wall.get(query_id)
    return wall_until is None or time.time() < wall_until


def _refresh(query_id: str, user_id: str | None) -> None:
    """Run one query and replace its cache entry. Never raises."""
    # Read before anything can fail, so every failure path can size its cooldown.
    interval_ms = get_settings().poll_interval_ms
    try:
        with Session(get_engine()) as session:
            query = session.get(SavedQuery, query_id)
            if query is None:
                return
            conn = session.get(Connection, query.connection_id)
            # A paused connection is disconnected on purpose. Refreshing it
            # behind the reader's back is exactly what "disconnected" rules out.
            if conn is None or conn.paused:
                return

            interval_ms = query_service.poll_interval_for(query)
            payload = query_service.run_saved_query(query, conn)
            # Logged like any other execution, and attributed like one. This
            # runs against the customer's database behind a response that has
            # already been sent, but somebody's poll is what started it -
            # load nobody can account for is the thing the execution log
            # exists to prevent.
            saved_query_service.log_execution(
                session,
                query.id,
                success=True,
                row_count=payload.row_count,
                duration_ms=payload.duration_ms,
                user_id=user_id,
            )
            interval = query_service.poll_interval_for(query)
            result_cache.set(
                query.id, payload.data_hash, payload.as_dict(interval), ttl_ms=interval
            )
            flagged_row_service.sync(
                session, query, payload.columns, payload.rows, payload.flags
            )
        with _lock:
            _cooldown_until.pop(query_id, None)
            _cooldown_wall.pop(query_id, None)
    except AppError as error:
        _hold_off(query_id, interval_ms)
        # A failed background run is still a run against their database, and the
        # execution log is where someone looks to find out why a card is stale.
        try:
            with Session(get_engine()) as session:
                saved_query_service.log_execution(
                    session, query_id, success=False, error=error, user_id=user_id
                )
        except Exception:  # noqa: BLE001 - logging must not raise either
            logger.debug("Could not record a failed refresh of %s", query_id)
        logger.info("Background refresh of query %s failed: %s", query_id, error.message)
    except Exception:  # noqa: BLE001 - a background refresh must not take a request with it
        _hold_off(query_id, interval_ms)
        # Logged at info: a target that is down is already reported to the
        # reader by the foreground path, and this would otherwise fill the log
        # once per poll interval per card for as long as it stays down.
        logger.info("Background refresh of query %s failed.", query_id, exc_info=True)
    finally:
        with _lock:
            _in_flight.discard(query_id)


def _hold_off(query_id: str, interval_ms: int) -> None:
    """Leave a query whose refresh just failed alone for one interval."""
    with _lock:
        hold_s = max(interval_ms, 1) / 1000
        _cooldown_until[query_id] = time.monotonic() + hold_s
        _cooldown_wall[query_id] = time.time() + hold_s


def reset() -> None:
    """Forget every cooldown. For tests, and for a config reload."""
    with _lock:
        _cooldown_until.clear()
        _cooldown_wall.clear()


def shutdown() -> None:
    """Stop the pool. Called from the app's lifespan."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
        _in_flight.clear()
        _cooldown_until.clear()
        _cooldown_wall.clear()
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def in_flight_count() -> int:
    """How many refreshes are running. For tests and diagnostics."""
    with _lock:
        return len(_in_flight)
