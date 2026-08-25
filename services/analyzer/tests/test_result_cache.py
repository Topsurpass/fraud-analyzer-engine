"""TTL cache behaviour, which is what makes polling cheap."""

from __future__ import annotations

import time

from app.config import get_settings
from app.services import result_cache


def setup_function() -> None:
    result_cache.clear()


def test_miss_on_empty_cache():
    assert result_cache.get("q1") is None


def test_hit_within_ttl():
    result_cache.set("q1", "sha256:abc", {"rows": []}, ttl_ms=5000)
    entry = result_cache.get("q1")
    assert entry is not None
    assert entry.data_hash == "sha256:abc"


def test_entry_expires_after_ttl():
    result_cache.set("q1", "sha256:abc", {"rows": []}, ttl_ms=10)
    time.sleep(0.05)
    assert result_cache.get("q1") is None


def test_an_expired_entry_is_kept_for_the_stale_path():
    """Expiry hides an entry from `get`; it does not throw it away.

    Serving the last known result while a fresh one is fetched behind it is
    what stops a poll blocking on the target database. Evicting on the way past
    would mean the stale path could never see the entry it exists to serve.
    Memory is still bounded: the byte budget evicts least-recently-used entries
    on write, and `get_stale` drops anything past the grace window.
    """
    result_cache.set("q1", "sha256:abc", {}, ttl_ms=10)
    time.sleep(0.05)

    assert result_cache.get("q1") is None
    assert result_cache.get_stale("q1") is not None
    assert result_cache.size() == 1


def test_an_entry_past_the_grace_window_is_dropped(monkeypatch):
    monkeypatch.setenv("FAE_CACHE_STALE_GRACE_MS", "1")
    get_settings.cache_clear()

    result_cache.set("q1", "sha256:abc", {}, ttl_ms=1)
    time.sleep(0.05)

    # Stale beats nothing, but "long ago" does not answer "what is happening
    # now", so it is dropped rather than served.
    assert result_cache.get_stale("q1") is None
    assert result_cache.size() == 0


def test_invalidate_drops_the_entry():
    result_cache.set("q1", "sha256:abc", {}, ttl_ms=5000)
    result_cache.invalidate("q1")
    assert result_cache.get("q1") is None


def test_invalidate_is_safe_on_a_missing_key():
    result_cache.invalidate("never-existed")


def test_set_overwrites():
    result_cache.set("q1", "old", {}, ttl_ms=5000)
    result_cache.set("q1", "new", {}, ttl_ms=5000)
    assert result_cache.get("q1").data_hash == "new"


def test_cache_is_bounded():
    for i in range(result_cache.MAX_ENTRIES + 20):
        result_cache.set(f"q{i}", "h", {}, ttl_ms=60_000)
    assert result_cache.size() == result_cache.MAX_ENTRIES


def test_eviction_drops_the_least_recently_used():
    for i in range(result_cache.MAX_ENTRIES):
        result_cache.set(f"q{i}", "h", {}, ttl_ms=60_000)
    result_cache.get("q0")  # refresh the oldest
    result_cache.set("new", "h", {}, ttl_ms=60_000)
    assert result_cache.get("q0") is not None
    assert result_cache.get("q1") is None


def test_clear_empties_everything():
    result_cache.set("q1", "h", {}, ttl_ms=5000)
    result_cache.clear()
    assert result_cache.size() == 0
