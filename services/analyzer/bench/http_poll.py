"""End-to-end HTTP cost of polling boards at 25,000 rows a query.

This is the number the user experiences, and the reference the fixes are
judged against. It exercises the real app: routing, auth, response models,
serialisation, the result cache and the batch endpoint.
"""

from __future__ import annotations

import os
import statistics
import time

os.environ["FAE_DB_BACKEND"] = "sqlite"
os.environ["FAE_AUTO_MIGRATE"] = "false"
os.environ["FAE_SQLITE_APP_DB_PATH"] = "/tmp/fae-bench/app.db"
os.environ["FAE_SQLITE_ALLOWED_DIRS"] = "/tmp/fae-bench"
os.environ.setdefault("FAE_MAX_ROW_LIMIT", "30000")
os.environ.setdefault("FAE_MAX_RESULT_BYTES", str(64 * 1024 * 1024))
os.environ.setdefault("FAE_RATE_LIMIT_PER_MINUTE", "0")
os.environ.setdefault("FAE_RATE_LIMIT_EXECUTION_PER_MINUTE", "0")

ROWS = int(os.environ.get("BENCH_ROWS", "25000"))
CARDS = int(os.environ.get("BENCH_CARDS", "8"))

SQL = """
SELECT occurred_at, terminal_id, merchant, mcc, amount, currency,
       status, card_bin, risk_score
FROM txns ORDER BY occurred_at LIMIT {n} OFFSET {off}
"""


def build():
    import pathlib

    for stale in ("/tmp/fae-bench/app.db",):
        pathlib.Path(stale).unlink(missing_ok=True)

    from fastapi.testclient import TestClient

    from app.db import app_state
    from app.main import app

    app_state.init_db()
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()

    from app.db.app_state import get_sessionmaker
    from app.models.enums import UserRole
    from app.security.passwords import hash_password
    from app.models.user import User

    session = get_sessionmaker()()
    session.add(
        User(
            email="bench@example.com",
            full_name="Bench",
            role=UserRole.ADMIN,
            password_hash=hash_password("Bench-Password-1!"),
            is_active=True,
            must_change_password=False,
        )
    )
    session.commit()
    session.close()

    token = client.post(
        "/auth/login",
        json={"email": "bench@example.com", "password": "Bench-Password-1!"},
    ).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    created = client.post(
        "/connections",
        json={"name": "bench", "db_type": "sqlite", "sqlite_path": "/tmp/fae-bench/target.db"},
    )
    assert created.status_code == 201, created.text
    conn_id = created.json()["connection"]["id"]

    query_ids = []
    for i in range(CARDS):
        made = client.post(
            f"/connections/{conn_id}/queries",
            json={
                "name": f"card {i}",
                "sql_text": SQL.format(n=ROWS, off=i * 1000),
                "row_limit": ROWS,
                "refresh_interval_s": 5,
            },
        )
        assert made.status_code == 201, made.text
        query_ids.append(made.json()["id"])
    return client, query_ids


def served(client, method, url, **kw):
    """Time the server only.

    Reading `.content` on a gzipped 2.6 MB response makes httpx decompress and
    buffer it, which lands in the same stopwatch as the handler and hides what
    the server actually did. Streaming stops at the response headers, which is
    the moment the server finished its work.
    """
    with client.stream(method, url, **kw) as response:
        return response


def timed(label, fn, repeat=3):
    """Report CPU time as well as wall clock.

    Wall clock on a shared machine is not a measurement. The same unchanged
    serial batch measured 2.5 s, 5.3 s, 24.0 s and 10.5 s across four runs here
    purely from other load. process_time counts only this process's own CPU,
    across all its threads, so it answers "what does this cost the server"
    rather than "what else was running". Wall clock is still printed, because
    it is the only thing that shows work happening in parallel: parallelism
    cuts wall clock while leaving CPU roughly flat.

    Minimum of N for both, for the same reason.
    """
    wall, cpu = [], []
    out = None
    for _ in range(repeat):
        w0, c0 = time.perf_counter(), time.process_time()
        out = fn()
        wall.append((time.perf_counter() - w0) * 1000)
        cpu.append((time.process_time() - c0) * 1000)
    print(f"{label:<48} {min(wall):8.1f} ms wall {min(cpu):8.1f} ms cpu")
    return out


def main():
    client, ids = build()
    print(f"{CARDS} cards x {ROWS:,} rows   (load average {os.getloadavg()[0]:.1f})\n")

    first = timed(
        "single /run, cold (target hit + full encode)",
        lambda: client.post(f"/queries/{ids[0]}/run"),
        repeat=3,
    )
    print(f"   response body: {len(first.content) / 1e6:.2f} MB, "
          f"content-encoding={first.headers.get('content-encoding', 'none')}")

    timed(
        "single /poll, warm cache  [server only]",
        lambda: served(client, "GET", f"/queries/{ids[0]}/poll"),
        repeat=7,
    )
    timed(
        "single /poll, warm cache  [+ client decode]",
        lambda: client.get(f"/queries/{ids[0]}/poll"),
        repeat=5,
    )
    hashed = client.get(f"/queries/{ids[0]}/poll").json()["data_hash"]
    timed(
        "single /poll, unchanged (since_hash matches)",
        lambda: client.get(f"/queries/{ids[0]}/poll", params={"since_hash": hashed}),
    )

    body = {"queries": [{"query_id": q} for q in ids], "force": False}
    cold = timed(
        f"batch /queries/poll, {CARDS} cards COLD (serial loop)",
        lambda: client.post("/queries/poll", json={**body, "force": True}),
        repeat=1,
    )
    print(f"   response body: {len(cold.content) / 1e6:.2f} MB")
    timed(
        f"batch /queries/poll, {CARDS} cards warm [server only]",
        lambda: served(client, "POST", "/queries/poll", json=body),
        repeat=5,
    )


    from app.services import rendered_cache

    stats = rendered_cache.stats()
    total = stats["hits"] + stats["misses"]
    print(
        f"\nrendered cache: {stats['hits']} hits / {total} lookups "
        f"({stats['hits'] * 100 // max(total, 1)}%), "
        f"{stats['entries']} entries, {stats['bytes'] / 1e6:.2f} MB held"
    )


if __name__ == "__main__":
    main()
