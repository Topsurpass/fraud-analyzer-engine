"""Build a realistic fraud-shaped SQLite target for the scale benchmarks.

Deterministic: same seed, same database, so a measurement taken today is
comparable to one taken after a change. The shape matters as much as the
size - the payload cost is per *cell*, and a table of five narrow integers
misrepresents what a real board pulls.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TERMINALS = [f"TERM{n:04d}" for n in range(120)]
MCC = ["5411", "5812", "5999", "6011", "7995", "4829"]
STATUS = ["approved", "declined", "reversed", "pending"]


def build(path: Path, rows: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute(
        """
        CREATE TABLE txns (
            id INTEGER PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            terminal_id TEXT NOT NULL,
            merchant TEXT NOT NULL,
            mcc TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            card_bin TEXT NOT NULL,
            risk_score REAL NOT NULL
        )
        """
    )
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    batch = []
    for i in range(rows):
        occurred = start + timedelta(seconds=i * 3)
        batch.append(
            (
                i,
                occurred.isoformat(),
                rng.choice(TERMINALS),
                f"Merchant {rng.randrange(400):03d} Ltd",
                rng.choice(MCC),
                round(rng.lognormvariate(3.2, 1.1), 2),
                "NGN",
                rng.choices(STATUS, weights=[70, 20, 5, 5])[0],
                str(rng.randrange(400000, 560000)),
                round(rng.random(), 4),
            )
        )
        if len(batch) >= 10_000:
            db.executemany("INSERT INTO txns VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        db.executemany("INSERT INTO txns VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    db.execute("CREATE INDEX idx_txns_occurred ON txns(occurred_at)")
    db.execute("CREATE INDEX idx_txns_terminal ON txns(terminal_id, occurred_at)")
    db.commit()
    db.close()


if __name__ == "__main__":
    target = Path(sys.argv[1])
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 250_000
    build(target, count)
    print(f"{target}: {count:,} rows, {target.stat().st_size / 1e6:.1f} MB")
