"""Payload and memory bounds.

row_limit says how many rows come back. It says nothing about how wide they
are, and nothing about how much the process then holds.
"""

from __future__ import annotations

import math

import pytest

from app.config import get_settings
from app.errors import ErrorCode, ResultTooLargeError
from app.services import result_cache
from app.services.query_service import canonical_hash, to_jsonable
from app.services.sizing import approx_json_size


# --------------------------------------------------------------------------
# Non-finite floats
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_become_null(value):
    """canonical_hash uses allow_nan=False, so these used to raise ValueError.

    ValueError is not an AppError, so the request became an opaque 500 *and*
    _execute_and_log's `except AppError` missed it, leaving no execution-log
    row either: the user saw a 500 and /logs showed nothing.
    """
    assert to_jsonable(value) is None


def test_a_row_containing_nan_still_hashes():
    columns = ["a", "b"]
    rows = [[to_jsonable(float("nan")), to_jsonable(1.5)]]
    assert canonical_hash(columns, rows).startswith("sha256:")


def test_ordinary_floats_are_untouched():
    for value in (0.0, -1.5, 1e308, 3.14159):
        assert to_jsonable(value) == value


def test_decimal_nan_survives_as_a_string():
    """Decimal takes the str() branch, so it is JSON-safe without being lost."""
    from decimal import Decimal

    assert to_jsonable(Decimal("NaN")) == "NaN"
    assert to_jsonable(Decimal("Infinity")) == "Infinity"


# --------------------------------------------------------------------------
# Result byte budget
# --------------------------------------------------------------------------


def test_wide_result_is_refused_before_it_is_materialised(
    client, sqlite_connection, monkeypatch
):
    """One row can be a gigabyte. SELECT repeat('x', 1e9) is the shape."""
    monkeypatch.setenv("FAE_MAX_RESULT_BYTES", "2000")
    get_settings.cache_clear()

    response = client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT replace(hex(zeroblob(5000)), '0', 'x') AS big"},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == ErrorCode.RESULT_TOO_LARGE.value


def test_a_result_inside_the_budget_is_returned(client, sqlite_connection, monkeypatch):
    monkeypatch.setenv("FAE_MAX_RESULT_BYTES", str(1024 * 1024))
    get_settings.cache_clear()

    response = client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT day, amount FROM txns"},
    )
    assert response.status_code == 200
    assert response.json()["row_count"] == 5


def test_size_estimate_tracks_actual_json_size():
    """The estimate only has to be within a small factor to serve as a budget."""
    import json

    payload = {"columns": ["a", "b"], "rows": [["x" * 100, 1] for _ in range(50)]}
    actual = len(json.dumps(payload).encode())
    estimated = approx_json_size(payload)
    assert 0.5 * actual < estimated < 2.0 * actual, (actual, estimated)


# --------------------------------------------------------------------------
# Cache memory bound
# --------------------------------------------------------------------------


def test_cache_evicts_on_bytes_not_entry_count(monkeypatch):
    """256 wide entries was roughly a gigabyte, on a container with 512 MB."""
    monkeypatch.setenv("FAE_CACHE_MAX_BYTES", "10000")
    get_settings.cache_clear()
    result_cache.clear()

    payload = {"rows": [["x" * 400]]}
    for i in range(50):
        result_cache.set(f"q{i}", f"h{i}", payload, ttl_ms=60_000)

    assert result_cache.total_bytes() <= 10000
    assert result_cache.size() < 50, "eviction never triggered"


def test_cache_stays_within_budget_under_churn(monkeypatch):
    monkeypatch.setenv("FAE_CACHE_MAX_BYTES", "5000")
    get_settings.cache_clear()
    result_cache.clear()

    for i in range(200):
        result_cache.set(f"q{i}", "h", {"rows": [["y" * 200]]}, ttl_ms=60_000)
        assert result_cache.total_bytes() <= 5000


def test_an_entry_larger_than_the_whole_budget_is_not_cached(monkeypatch):
    """Caching it would evict everything else and still not fit."""
    monkeypatch.setenv("FAE_CACHE_MAX_BYTES", "1000")
    get_settings.cache_clear()
    result_cache.clear()

    result_cache.set("small", "h", {"rows": [["a"]]}, ttl_ms=60_000)
    entry = result_cache.set("huge", "h", {"rows": [["z" * 5000]]}, ttl_ms=60_000)

    assert entry.data_hash == "h"          # caller still gets its entry back
    assert result_cache.get("huge") is None  # but nothing was stored
    assert result_cache.get("small") is not None, "an oversized set evicted the cache"


def test_byte_accounting_is_returned_on_invalidate(monkeypatch):
    monkeypatch.setenv("FAE_CACHE_MAX_BYTES", str(1024 * 1024))
    get_settings.cache_clear()
    result_cache.clear()

    result_cache.set("q", "h", {"rows": [["a" * 100]]}, ttl_ms=60_000)
    assert result_cache.total_bytes() > 0
    result_cache.invalidate("q")
    assert result_cache.total_bytes() == 0


def test_overwriting_an_entry_does_not_double_count(monkeypatch):
    monkeypatch.setenv("FAE_CACHE_MAX_BYTES", str(1024 * 1024))
    get_settings.cache_clear()
    result_cache.clear()

    for _ in range(10):
        result_cache.set("q", "h", {"rows": [["a" * 100]]}, ttl_ms=60_000)

    assert result_cache.size() == 1
    assert result_cache.total_bytes() < 500, result_cache.total_bytes()


def test_expiry_returns_its_bytes(monkeypatch):
    monkeypatch.setenv("FAE_CACHE_MAX_BYTES", str(1024 * 1024))
    get_settings.cache_clear()
    result_cache.clear()

    result_cache.set("q", "h", {"rows": [["a" * 100]]}, ttl_ms=0)
    assert result_cache.get("q") is None
    assert result_cache.total_bytes() == 0
