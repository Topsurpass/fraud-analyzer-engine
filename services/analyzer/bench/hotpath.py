"""Where the time goes on one 25,000-row poll, stage by stage.

Runs the real functions, not a model of them. Every stage is reported
separately because the fix for each is different, and a single end-to-end
number hides which one to attack.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

os.environ.setdefault("FAE_DB_BACKEND", "sqlite")
os.environ.setdefault("FAE_AUTO_MIGRATE", "false")

DB = Path("/tmp/fae-bench/target.db")
ROWS = int(os.environ.get("BENCH_ROWS", "25000"))

SQL = f"""
SELECT occurred_at, terminal_id, merchant, mcc, amount, currency,
       status, card_bin, risk_score
FROM txns ORDER BY occurred_at LIMIT {ROWS}
"""


def timed(label, fn, repeat=5):
    samples = []
    out = None
    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        samples.append((time.perf_counter() - start) * 1000)
    print(f"{label:<44} {statistics.median(samples):8.1f} ms   (min {min(samples):.1f})")
    return out


def main():
    import sqlite3

    from app.services.query_service import canonical_hash, to_jsonable
    from app.services.sizing import approx_json_size

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    raw = timed("1. sqlite fetch (raw driver)", lambda: db.execute(SQL).fetchall())
    columns = [
        "occurred_at", "terminal_id", "merchant", "mcc", "amount",
        "currency", "status", "card_bin", "risk_score",
    ]
    print(f"   {len(raw):,} rows x {len(columns)} cols = {len(raw) * len(columns):,} cells")

    coerced = timed(
        "2. to_jsonable, per cell", lambda: [[to_jsonable(v) for v in r] for r in raw]
    )
    timed(
        "3. approx_json_size, per cell (budget check)",
        lambda: sum(sum(approx_json_size(v) for v in r) for r in coerced),
    )
    timed("4. canonical_hash (json.dumps sort_keys + sha256)",
          lambda: canonical_hash(columns, coerced, {}))

    body = {
        "query_id": "q", "executed_at": "2026-09-02T00:00:00Z", "duration_ms": 1,
        "row_count": len(coerced), "truncated": False, "data_hash": "sha256:x",
        "columns": columns, "rows": coerced, "charts": [],
        "poll_interval_ms": 5000,
    }
    timed("5. approx_json_size on the cached body", lambda: approx_json_size(body))
    blob = timed("6. json.dumps of the response body", lambda: json.dumps(body))
    print(f"   wire size: {len(blob) / 1e6:.2f} MB uncompressed")

    try:
        import orjson

        timed("6b. orjson.dumps of the same body", lambda: orjson.dumps(body))
    except ImportError:
        print("6b. orjson                                    not installed")

    from fastapi.encoders import jsonable_encoder

    timed("7. fastapi jsonable_encoder on the body",
          lambda: jsonable_encoder(body), repeat=3)


if __name__ == "__main__":
    main()
