"""Cache of fully rendered poll responses: JSON encoded and gzipped, once.

## Why this exists

``result_cache`` stops the target database being re-queried. It does not stop
the *answer* being rebuilt, and at the scale this service is aimed at that is
where the time goes. Measured on a 25,000-row by 9-column result, a poll that
hit a warm ``result_cache`` still cost 131 ms, of which almost none was the
lookup: it was re-validating the payload against the response model,
re-serialising 225,000 cells to JSON, and re-gzipping 2.87 MB.

That work is identical every time, because the thing being rendered is
identical every time. A result is immutable once executed - it is keyed by a
hash of its own contents - so the bytes for a given ``data_hash`` can only ever
be one thing. Rebuilding them per poll is pure waste, and it is waste that
scales the wrong way: it is paid per *viewer*, per *interval*.

That is the "many analysts" case exactly. Ten analysts watching one board on a
five-second interval is 120 renders a minute of byte-for-byte identical
output. With this cache it is one, and the other 119 are a dict lookup and a
socket write.

## Why gzipped bytes rather than a dict

Storing the encoded form is what removes the serialisation; storing the
compressed form is what removes the compression. Both are per-response costs
today and neither depends on who is asking. It also *shrinks* what is held:
gzip level 1 on this payload shape is about 4.5x, so an entry here costs
roughly a fifth of the same result sitting in ``result_cache`` as a dict.

A client that will not take gzip is served by decompressing on the way out.
Every browser sends ``Accept-Encoding: gzip``, so that path is for curl and
for tests, and it is still cheaper than re-encoding from the dict.

## Why keying on the served hash is safe

``data_hash`` is a sha256 over the columns, rows and flags that the client
receives, and ``flag_dismissal_service.apply_dismissals`` deliberately mixes
the dismissal state into it. So two responses with the same served hash are
the same response, including which rows are marked. The rest of the key is
what the hash does not cover: the poll interval, and whether the payload is
being reported as served from cache.
"""

from __future__ import annotations

import gzip
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass

import orjson

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Entries are large and few. The byte budget below is the real bound; this
#: only stops a flood of tiny results growing the dict without limit.
MAX_ENTRIES = 256

#: Level 1 for the same reason the response middleware uses it: on this payload
#: shape it reaches 4.5x, and the levels above it buy single-digit percentages
#: for multiples of the CPU. See the table in app/main.py.
GZIP_LEVEL = 1

#: Below this, compression is not worth the header handshake and the entry is
#: stored as plain JSON. Matches the middleware's minimum_size.
MIN_COMPRESS_BYTES = 1024


@dataclass(slots=True)
class Rendered:
    """One response, ready to write to a socket."""

    body: bytes
    #: True when ``body`` is gzip-compressed. False for payloads too small to
    #: be worth it, which are served as-is to any client.
    compressed: bool


_entries: OrderedDict[str, Rendered] = OrderedDict()
_lock = threading.RLock()
_total_bytes = 0

#: Hit and miss counts. The point of this cache is that N analysts watching one
#: board cost what one analyst costs, and that claim is a ratio, not a
#: stopwatch: it holds or it does not, independent of how loaded the machine
#: is. Timings drift, this does not, so it is what the tests assert on and what
#: /metrics reports.
_hits = 0
_misses = 0


def key_for(query_id: str, data_hash: str, poll_interval_ms: int, from_cache: bool) -> str:
    """The identity of a rendered response.

    ``data_hash`` alone would be wrong: the same rows served with a different
    poll interval, or reported with a different ``from_cache``, are different
    bytes. Including ``query_id`` keeps two queries that happen to return
    identical data from sharing an entry, which would make eviction of one
    silently affect the other.
    """
    return f"{query_id}|{data_hash}|{poll_interval_ms}|{int(from_cache)}"


def get(key: str) -> Rendered | None:
    global _hits, _misses
    with _lock:
        found = _entries.get(key)
        if found is None:
            _misses += 1
            return None
        _hits += 1
        _entries.move_to_end(key)
        return found


def render(body: dict) -> Rendered:
    """Encode and compress a response body. Does not store it."""
    raw = orjson.dumps(body)
    if len(raw) < MIN_COMPRESS_BYTES:
        return Rendered(body=raw, compressed=False)
    return Rendered(body=gzip.compress(raw, compresslevel=GZIP_LEVEL), compressed=True)


def store(key: str, rendered: Rendered) -> Rendered:
    """Hold a rendered response, evicting least-recently-used to stay in budget."""
    global _total_bytes
    budget = get_settings().rendered_cache_max_bytes
    size = len(rendered.body)

    with _lock:
        existing = _entries.pop(key, None)
        if existing is not None:
            _total_bytes -= len(existing.body)

        # One entry larger than the whole budget would evict everything else
        # and still not fit. Hand it back unstored; the request is unaffected.
        if size > budget:
            logger.warning(
                "Rendered response for %s is %d bytes, over the %d byte budget; "
                "not caching it.",
                key,
                size,
                budget,
            )
            return rendered

        _entries[key] = rendered
        _total_bytes += size
        _entries.move_to_end(key)

        while _entries and (_total_bytes > budget or len(_entries) > MAX_ENTRIES):
            oldest, evicted = _entries.popitem(last=False)
            _total_bytes -= len(evicted.body)
            if oldest == key:  # pragma: no cover - guarded by the size check above
                break
    return rendered


def get_or_render(key: str, build: "callable[[], dict]") -> Rendered:
    """Return the cached bytes for ``key``, rendering and storing them if absent.

    ``build`` is a callable rather than a dict so a hit never pays for
    assembling a body it is not going to encode.
    """
    found = get(key)
    if found is not None:
        return found
    return store(key, render(build()))


def invalidate_query(query_id: str) -> None:
    """Drop every rendered response belonging to one query.

    Called wherever ``result_cache.invalidate`` is: a query whose SQL or chart
    mapping changed must not keep serving bytes built from the old one.
    """
    global _total_bytes
    prefix = f"{query_id}|"
    with _lock:
        for key in [k for k in _entries if k.startswith(prefix)]:
            _total_bytes -= len(_entries.pop(key).body)


def stats() -> dict[str, int]:
    """Hits, misses, entries and bytes held."""
    with _lock:
        return {
            "hits": _hits,
            "misses": _misses,
            "entries": len(_entries),
            "bytes": _total_bytes,
        }


def clear() -> None:
    global _total_bytes, _hits, _misses
    with _lock:
        _entries.clear()
        _total_bytes = 0
        _hits = 0
        _misses = 0


def size() -> int:
    with _lock:
        return len(_entries)


def total_bytes() -> int:
    """Bytes currently held. Exposed so a test can assert the bound."""
    with _lock:
        return _total_bytes
