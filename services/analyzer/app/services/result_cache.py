"""Short-TTL cache of the last result per saved query.

This is what makes ``/poll`` cheap. Without it a frontend polling every five
seconds would run 720 real queries per hour per chart against the customer's
production database. With it, a poll inside the TTL compares hashes in memory
and never opens a connection.

The cache is per-process and deliberately not shared. It holds query results
from a customer database, so it stays in memory, is bounded, and dies with the
process. ``/run`` always bypasses it, so there is always a way to force a fresh
read.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

MAX_ENTRIES = 256


@dataclass(slots=True)
class CacheEntry:
    data_hash: str
    payload: Any
    stored_at: float
    ttl_ms: int

    def is_fresh(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.stored_at) * 1000 < self.ttl_ms


_entries: OrderedDict[str, CacheEntry] = OrderedDict()
_lock = threading.RLock()


def get(query_id: str) -> CacheEntry | None:
    """Return the cached entry if it is still within its TTL, else ``None``."""
    with _lock:
        entry = _entries.get(query_id)
        if entry is None:
            return None
        if not entry.is_fresh():
            del _entries[query_id]
            return None
        _entries.move_to_end(query_id)
        return entry


def set(query_id: str, data_hash: str, payload: Any, ttl_ms: int) -> CacheEntry:
    entry = CacheEntry(
        data_hash=data_hash, payload=payload, stored_at=time.monotonic(), ttl_ms=ttl_ms
    )
    with _lock:
        _entries[query_id] = entry
        _entries.move_to_end(query_id)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
    return entry


def invalidate(query_id: str) -> None:
    """Drop a query's entry. Call whenever its SQL or chart mapping changes."""
    with _lock:
        _entries.pop(query_id, None)


def clear() -> None:
    with _lock:
        _entries.clear()


def size() -> int:
    with _lock:
        return len(_entries)
