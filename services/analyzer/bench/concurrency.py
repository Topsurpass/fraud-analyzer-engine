"""Many analysts watching the same board at once, against the real server.

Not TestClient: this goes over a socket to uvicorn in its container, because
the question is what happens when requests actually overlap - worker threads,
the app-state pool, and whether one analyst's poll makes another's slower.

The shape is the realistic one. Analysts do not each watch their own private
data; they watch the same fraud board. So the interesting number is whether
the Nth concurrent viewer costs anything, which is exactly what serving
pre-rendered bytes is supposed to make true.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = os.environ.get("BENCH_BASE", "http://localhost:8000")
EMAIL = os.environ["BENCH_EMAIL"]
PASSWORD = os.environ["BENCH_PASSWORD"]
QUERY_ID = os.environ["BENCH_QUERY_ID"]
ROUNDS = int(os.environ.get("BENCH_ROUNDS", "12"))


def token() -> str:
    response = httpx.post(
        f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30
    )
    response.raise_for_status()
    return response.json()["token"]


def one_analyst(auth: dict, rounds: int) -> list[float]:
    """One analyst's poll loop, timed per poll."""
    samples = []
    with httpx.Client(base_url=BASE, headers=auth, timeout=60) as client:
        for _ in range(rounds):
            start = time.perf_counter()
            response = client.get(f"/queries/{QUERY_ID}/poll")
            response.read()
            samples.append((time.perf_counter() - start) * 1000)
            if response.status_code != 200:
                print(f"  ! {response.status_code} {response.text[:120]}", file=sys.stderr)
    return samples


def main() -> None:
    auth = {"Authorization": f"Bearer {token()}"}

    # Warm the result and its rendering, so this measures steady-state
    # watching rather than one cold execution shared by everybody.
    httpx.post(f"{BASE}/queries/{QUERY_ID}/run", headers=auth, timeout=120).raise_for_status()

    print(f"{'analysts':>9}{'polls':>8}{'p50':>9}{'p95':>9}{'max':>9}{'req/s':>9}")
    for analysts in (1, 2, 5, 10, 20):
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=analysts) as pool:
            batches = list(pool.map(lambda _: one_analyst(auth, ROUNDS), range(analysts)))
        elapsed = time.perf_counter() - started
        samples = sorted(s for batch in batches for s in batch)
        p50 = statistics.median(samples)
        p95 = samples[int(len(samples) * 0.95) - 1]
        print(
            f"{analysts:>9}{len(samples):>8}{p50:>8.1f}m{p95:>8.1f}m"
            f"{max(samples):>8.1f}m{len(samples) / elapsed:>9.0f}"
        )


if __name__ == "__main__":
    main()
