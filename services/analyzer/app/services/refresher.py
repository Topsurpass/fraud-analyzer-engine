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
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.db.app_state import get_engine
from app.errors import AppError
from app.models import Connection, SavedQuery
from app.services import (
    flagged_row_service,
    query_service,
    result_cache,
    saved_query_service,
)

logger = logging.getLogger(__name__)

#: Small on purpose. This exists to keep a page responsive, not to fan out.
_MAX_WORKERS = 4

_pool: ThreadPoolExecutor | None = None
_in_flight: set[str] = set()
_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, thread_name_prefix="fae-refresh"
            )
        return _pool


def request_refresh(query_id: str) -> bool:
    """Ask for a background refresh. Returns whether one was actually started.

    False means one is already running for this query, which is the common case
    on a dashboard: every card watching a stale query asks at once and exactly
    one execution follows.
    """
    with _lock:
        if query_id in _in_flight:
            return False
        _in_flight.add(query_id)

    try:
        _get_pool().submit(_refresh, query_id)
        return True
    except RuntimeError:  # pragma: no cover - pool shut down mid-request
        with _lock:
            _in_flight.discard(query_id)
        return False


def _refresh(query_id: str) -> None:
    """Run one query and replace its cache entry. Never raises."""
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

            payload = query_service.run_saved_query(query, conn)
            # Logged like any other execution. This runs against the customer's
            # database without anyone asking it to, and load nobody can see in
            # the execution log is load nobody can account for.
            saved_query_service.log_execution(
                session,
                query.id,
                success=True,
                row_count=payload.row_count,
                duration_ms=payload.duration_ms,
            )
            interval = query_service.poll_interval_for(query)
            result_cache.set(
                query.id, payload.data_hash, payload.as_dict(interval), ttl_ms=interval
            )
            flagged_row_service.sync(
                session, query, payload.columns, payload.rows, payload.flags
            )
    except AppError as error:
        # A failed background run is still a run against their database, and the
        # execution log is where someone looks to find out why a card is stale.
        try:
            with Session(get_engine()) as session:
                saved_query_service.log_execution(
                    session, query_id, success=False, error=error
                )
        except Exception:  # noqa: BLE001 - logging must not raise either
            logger.debug("Could not record a failed refresh of %s", query_id)
        logger.info("Background refresh of query %s failed: %s", query_id, error.message)
    except Exception:  # noqa: BLE001 - a background refresh must not take a request with it
        # Logged at info: a target that is down is already reported to the
        # reader by the foreground path, and this would otherwise fill the log
        # once per poll interval per card for as long as it stays down.
        logger.info("Background refresh of query %s failed.", query_id, exc_info=True)
    finally:
        with _lock:
            _in_flight.discard(query_id)


def shutdown() -> None:
    """Stop the pool. Called from the app's lifespan."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
        _in_flight.clear()
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def in_flight_count() -> int:
    """How many refreshes are running. For tests and diagnostics."""
    with _lock:
        return len(_in_flight)
