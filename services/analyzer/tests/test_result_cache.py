"""TTL cache behaviour, which is what makes polling cheap."""

from __future__ import annotations

import time

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


def test_expired_entry_is_evicted_not_just_hidden():
    result_cache.set("q1", "sha256:abc", {}, ttl_ms=10)
    time.sleep(0.05)
    result_cache.get("q1")
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
