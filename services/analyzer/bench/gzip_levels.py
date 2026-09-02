"""What each gzip level actually costs and saves on a real 25k-row payload.

Transfer time assumes a 20 Mbps analyst link, which is the point: the CPU is
spent on the server once, the bytes are paid by every viewer on every poll.
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import statistics
import time

ROWS = int(os.environ.get("BENCH_ROWS", "25000"))
LINK_MBPS = 20

db = sqlite3.connect("file:/tmp/fae-bench/target.db?mode=ro", uri=True)
raw = db.execute(
    f"SELECT occurred_at, terminal_id, merchant, mcc, amount, currency, status,"
    f" card_bin, risk_score FROM txns ORDER BY occurred_at LIMIT {ROWS}"
).fetchall()
body = json.dumps({"rows": [list(r) for r in raw]}).encode()
print(f"{ROWS:,} rows, {len(body)/1e6:.2f} MB raw\n")
print(f"{'level':<7}{'size':>10}{'ratio':>8}{'compress':>11}{'xfer@20Mb':>12}{'total':>9}")

plain_ms = len(body) * 8 / (LINK_MBPS * 1e6) * 1000
print(f"{'none':<7}{len(body)/1e6:9.2f}M{1.0:8.1f}{0.0:10.1f}m{plain_ms:11.0f}m{plain_ms:8.0f}m")

for level in (1, 2, 3, 5, 6, 9):
    samples = []
    for _ in range(3):
        t = time.perf_counter()
        out = gzip.compress(body, compresslevel=level)
        samples.append((time.perf_counter() - t) * 1000)
    ms = statistics.median(samples)
    xfer = len(out) * 8 / (LINK_MBPS * 1e6) * 1000
    print(f"{level:<7}{len(out)/1e6:9.2f}M{len(body)/len(out):8.1f}{ms:10.1f}m{xfer:11.0f}m{ms+xfer:8.0f}m")
