"""Short-TTL cache of the last result per saved query.

This is what makes ``/poll`` cheap. Without it a frontend polling every five
seconds would run 720 real queries per hour per chart against the customer's
production database. With it, a poll inside the TTL compares hashes in memory
and never opens a connection.

The cache is per-process and deliberately not shared. It holds query results
from a customer database, so it stays in memory, is bounded, and dies with the
process. ``/run`` always bypasses it, so there is always a way to force a fresh
read.

**The bound is bytes, not entries.** Counting entries says nothing about what
an entry costs. One 10,000-row by 5-narrow-column result measured at 1.1 MB, so
the old 256-entry ceiling was really a 0.27 GB ceiling for narrow results and
roughly 1 GB for wide ones. A 512 MB container is OOM-killed long before
eviction ever triggers, which turns a cache that exists to protect the target
database into the thing that kills the service. Entries are still capped too,
so a flood of tiny results cannot grow the dict without limit.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services.sizing import approx_json_size

logger = logging.getLogger(__name__)

MAX_ENTRIES = 256


@dataclass(slots=True)
class CacheEntry:
    data_hash: str
    payload: Any
    stored_at: float
    ttl_ms: int
    size_bytes: int = 0

    def is_fresh(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.stored_at) * 1000 < self.ttl_ms

    def age_ms(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        return int((now - self.stored_at) * 1000)


_entries: OrderedDict[str, CacheEntry] = OrderedDict()
_lock = threading.RLock()
_total_bytes = 0


def _drop_locked(query_id: str) -> None:
    """Remove one entry and give back its bytes. Caller holds the lock."""
    global _total_bytes
    entry = _entries.pop(query_id, None)
    if entry is not None:
        _total_bytes -= entry.size_bytes


def get(query_id: str) -> CacheEntry | None:
    """Return the cached entry if it is still within its TTL, else ``None``.

    A stale entry is left in place rather than evicted here. ``get_stale`` is
    what decides whether it is still worth serving, and dropping it on the way
    past would mean the stale path could never see it - which is exactly the
    bug this comment exists to stop someone reintroducing.
    """
    with _lock:
        entry = _entries.get(query_id)
        if entry is None:
            return None
        if not entry.is_fresh():
            return None
        _entries.move_to_end(query_id)
        return entry


def get_stale(query_id: str) -> CacheEntry | None:
    """Return the cached entry even if its TTL has passed.

    What makes a chart appear instantly instead of after a round trip to the
    customer's database. A poll past the TTL used to run the query inline, so
    with a five-second TTL and a two-second query roughly half of all polls
    blocked - and the card sat empty while they did.

    Serving the previous answer and refreshing behind it is the honest trade:
    the data on screen is at most one interval old, and it says how old through
    ``executed_at``, where before it was simply absent. Entries past the grace
    window are dropped, because "an hour ago" is not a useful answer to "what
    is happening now".
    """
    with _lock:
        entry = _entries.get(query_id)
        if entry is None:
            return None
        grace = get_settings().cache_stale_grace_ms
        if entry.age_ms() > entry.ttl_ms + grace:
            _drop_locked(query_id)
            return None
        _entries.move_to_end(query_id)
        return entry


def set(query_id: str, data_hash: str, payload: Any, ttl_ms: int) -> CacheEntry:
    """Cache a payload, evicting least-recently-used entries to stay in budget.

    A payload larger than the whole budget is measured and then not stored:
    caching it would evict everything else and still not fit. The caller still
    gets its entry back so the request behaves normally.
    """
    global _total_bytes
    budget = get_settings().cache_max_bytes
    size_bytes = approx_json_size(payload)
    entry = CacheEntry(
        data_hash=data_hash,
        payload=payload,
        stored_at=time.monotonic(),
        ttl_ms=ttl_ms,
        size_bytes=size_bytes,
    )

    with _lock:
        _drop_locked(query_id)

        if size_bytes > budget:
            logger.warning(
                "Result for query %s is %d bytes, over the %d byte cache "
                "budget; not caching it.",
                query_id,
                size_bytes,
                budget,
            )
            return entry

        _entries[query_id] = entry
        _total_bytes += size_bytes
        _entries.move_to_end(query_id)

        while _entries and (_total_bytes > budget or len(_entries) > MAX_ENTRIES):
            oldest, evicted = _entries.popitem(last=False)
            _total_bytes -= evicted.size_bytes
            if oldest == query_id:  # pragma: no cover - guarded by the check above
                break
    return entry


def invalidate(query_id: str) -> None:
    """Drop a query's entry. Call whenever its SQL or chart mapping changes."""
    with _lock:
        _drop_locked(query_id)


def clear() -> None:
    global _total_bytes
    with _lock:
        _entries.clear()
        _total_bytes = 0


def size() -> int:
    with _lock:
        return len(_entries)


def total_bytes() -> int:
    """Approximate bytes currently held. Exposed so a test can assert the bound."""
    with _lock:
        return _total_bytes
