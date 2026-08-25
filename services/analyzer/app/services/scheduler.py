"""Runs rule-bearing queries on their own interval, with nobody watching.

Until this existed a saved query only ran when a browser polled it, and the
browser stopped polling when the tab was hidden. Flagged findings therefore
only appeared for the minutes somebody happened to be looking at a dashboard,
which is the opposite of what a review queue is for.

This is the one part of the service that touches a customer's production
database unprompted, so every bound on it is deliberate:

* **Only queries that define flag rules.** A chart with no rules produces
  nothing to review, so running it on a timer would be load with no output.
* **Only when it is actually due.** Each query's own ``poll_interval_ms``, with
  a floor from ``FAE_SCHEDULER_MIN_INTERVAL_MS`` so a query saved with a
  one-second interval cannot be turned into a denial of service by a typo.
* **One at a time.** Sequential rather than a fan-out: twenty saved queries
  firing together is a load spike on someone's production server, and there is
  no deadline here that justifies it.
* **Backoff on failure.** A query whose target is down doubles its own delay up
  to ``FAE_SCHEDULER_MAX_BACKOFF_MS`` rather than retrying at full rate against
  a database that is already unhappy.
* **Switchable off.** ``FAE_SCHEDULER_ENABLED=false`` and the service behaves
  exactly as it did before.

State is in memory. A restart re-runs everything once, which is correct: the
engine has no idea how long it was down or what changed while it was.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db.app_state import get_engine
from app.models import Connection, FlagRule, SavedQuery
from app.services import flagged_row_service, query_service, result_cache

logger = logging.getLogger(__name__)

#: Per query: the epoch-ms after which it may run again.
_next_due: dict[str, float] = {}
#: Per query: how much its interval is currently multiplied by, after failures.
_backoff: dict[str, int] = {}


def _now_ms() -> float:
    return time.monotonic() * 1000


def _due_queries(session: Session) -> list[SavedQuery]:
    """Rule-bearing queries whose interval has elapsed.

    Eager-loads the rules, because evaluating a run needs them and a lazy load
    per query would be one extra statement per card on every tick.
    """
    statement = select(SavedQuery).options(
        selectinload(SavedQuery.flag_rules).selectinload(FlagRule.conditions)
    )
    now = _now_ms()
    due = []
    for query in session.scalars(statement):
        if not query.flag_rules:
            continue
        if _next_due.get(query.id, 0) <= now:
            due.append(query)
    return due


def interval_for(query: SavedQuery) -> int:
    """How often this query may run, honouring the configured floor."""
    settings = get_settings()
    wanted = query_service.poll_interval_for(query)
    return max(wanted, settings.scheduler_min_interval_ms)


def _reschedule(query: SavedQuery, *, failed: bool) -> None:
    settings = get_settings()
    base = interval_for(query)
    if failed:
        # Double each time, so a target that is down is asked less and less
        # often rather than at full rate forever.
        factor = min(max(_backoff.get(query.id, 1) * 2, 2), 1 << 16)
        _backoff[query.id] = factor
        delay = min(base * factor, settings.scheduler_max_backoff_ms)
    else:
        _backoff.pop(query.id, None)
        delay = base
    _next_due[query.id] = _now_ms() + delay


def run_due_once(session: Session) -> int:
    """Run every query that is due. Returns how many actually ran.

    Synchronous and sequential on purpose; the caller runs it off the event
    loop. Every failure is caught and turned into backoff: a scheduler that
    dies on one unreachable database stops watching every other one too.
    """
    ran = 0
    for query in _due_queries(session):
        conn = session.get(Connection, query.connection_id)
        if conn is None:  # pragma: no cover - FK makes this unreachable
            continue
        if conn.paused:
            # Disconnected on purpose. Skipped rather than failed: this is not
            # an error to back off from, and it must not fill the log every
            # tick for as long as someone leaves a connection off.
            continue
        try:
            payload = query_service.run_saved_query(query, conn)
        except Exception:  # noqa: BLE001 - one bad target must not stop the rest
            logger.warning(
                "Scheduled run of query %s (%s) failed; backing off.",
                query.id,
                query.name,
                exc_info=True,
            )
            _reschedule(query, failed=True)
            continue

        interval = query_service.poll_interval_for(query)
        result_cache.set(
            query.id, payload.data_hash, payload.as_dict(interval), ttl_ms=interval
        )
        flagged_row_service.sync(
            session, query, payload.columns, payload.rows, payload.flags
        )
        _reschedule(query, failed=False)
        ran += 1
    return ran


async def run_forever(stop: asyncio.Event) -> None:
    """The loop itself. Ticks on the configured cadence until asked to stop."""
    settings = get_settings()
    logger.info(
        "Flag scheduler started: tick %dms, minimum query interval %dms.",
        settings.scheduler_tick_ms,
        settings.scheduler_min_interval_ms,
    )
    while not stop.is_set():
        try:
            # A thread, because everything below it is blocking SQLAlchemy and
            # this service's event loop also serves requests.
            ran = await asyncio.to_thread(_tick)
            if ran:
                logger.info("Scheduler ran %d due quer%s.", ran, "y" if ran == 1 else "ies")
        except Exception:  # noqa: BLE001 - the loop outlives any single tick
            logger.exception("Scheduler tick failed; continuing.")

        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.scheduler_tick_ms / 1000
            )
        except TimeoutError:
            continue
    logger.info("Flag scheduler stopped.")


def _tick() -> int:
    with Session(get_engine()) as session:
        return run_due_once(session)


def reset() -> None:
    """Forget every schedule. For tests, and for a config reload."""
    _next_due.clear()
    _backoff.clear()
