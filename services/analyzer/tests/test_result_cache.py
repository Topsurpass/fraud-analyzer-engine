"""TTL cache behaviour, which is what makes polling cheap."""

from __future__ import annotations

import time

from app.config import get_settings
from app.features.queries import result_cache


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


def _payload(charts):
    return {"columns": ["day", "n"], "rows": [["a", 1]], "charts": charts, "data_hash": "sha256:abc"}


def test_patch_charts_swaps_only_the_mapping():
    result_cache.set("q1", "sha256:abc", _payload([{"type": "line"}]), ttl_ms=5000)
    before = result_cache.get("q1")
    stored_at, hash_ = before.stored_at, before.data_hash

    assert result_cache.patch_charts("q1", lambda columns: [{"type": "bar", "cols": columns}]) is True

    entry = result_cache.get("q1")
    assert entry.payload["charts"] == [{"type": "bar", "cols": ["day", "n"]}]
    # Rows, hash and age are untouched: the schedule keeps counting from the last run.
    assert entry.payload["rows"] == [["a", 1]]
    assert entry.data_hash == hash_
    assert entry.stored_at == stored_at
    assert entry.ttl_ms == 5000


def test_patch_charts_keeps_the_byte_budget_honest():
    result_cache.set("q1", "h", _payload([]), ttl_ms=5000)
    small = result_cache.get("q1").size_bytes
    result_cache.patch_charts("q1", lambda columns: [{"type": "bar", "pad": "x" * 500}])
    grown = result_cache.get("q1").size_bytes
    assert grown > small
    assert result_cache.total_bytes() == grown
    result_cache.patch_charts("q1", lambda columns: [])
    assert result_cache.total_bytes() == small


def test_patch_charts_without_an_entry_is_a_no_op():
    assert result_cache.patch_charts("nope", lambda columns: [{"type": "bar"}]) is False
    assert result_cache.get("nope") is None


def test_patch_charts_also_patches_a_stale_entry_that_is_still_served():
    result_cache.set("q1", "h", _payload([{"type": "line"}]), ttl_ms=10)
    time.sleep(0.05)
    assert result_cache.get("q1") is None  # stale, past its TTL
    assert result_cache.patch_charts("q1", lambda columns: [{"type": "bar"}]) is True
    assert result_cache.get_stale("q1").payload["charts"] == [{"type": "bar"}]


def test_patch_charts_drops_the_entry_rather_than_keep_a_wrong_mapping():
    result_cache.set("q1", "h", _payload([{"type": "line"}]), ttl_ms=5000)

    def boom(columns):
        raise ValueError("cannot build")

    assert result_cache.patch_charts("q1", boom) is False
    assert result_cache.get("q1") is None
    assert result_cache.total_bytes() == 0


def test_patch_charts_drops_the_rendered_bytes_built_from_the_old_mapping(monkeypatch):
    from app.features.queries import rendered_cache

    dropped = []
    monkeypatch.setattr(rendered_cache, "invalidate_query", lambda query_id: dropped.append(query_id))
    result_cache.set("q1", "h", _payload([{"type": "line"}]), ttl_ms=5000)
    result_cache.patch_charts("q1", lambda columns: [{"type": "bar"}])
    result_cache.patch_charts("missing", lambda columns: [])
    assert dropped == ["q1", "missing"]
