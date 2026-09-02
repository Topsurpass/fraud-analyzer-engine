"""cProfile one 8-card warm batch, to find what the workers actually spend on."""
from __future__ import annotations

import cProfile
import io
import os
import pstats

os.environ["BENCH_CARDS"] = os.environ.get("BENCH_CARDS", "8")

from http_poll import build  # noqa: E402

client, ids = build()
body = {"queries": [{"query_id": q} for q in ids], "force": False}
client.post("/queries/poll", json=body)  # warm everything

profiler = cProfile.Profile()
profiler.enable()
client.post("/queries/poll", json=body)
profiler.disable()

buf = io.StringIO()
pstats.Stats(profiler, stream=buf).sort_stats("cumulative").print_stats(22)
print(buf.getvalue())
