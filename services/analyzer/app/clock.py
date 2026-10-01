"""Elapsed time that survives a paused machine.

``time.monotonic()`` is the right clock for "how long since", except where the
process runs on a machine that gets suspended. Docker Desktop on a laptop is the
common case: when the host sleeps the VM is paused, and the VM's monotonic clock
stops with it. Measured on a developer's machine, a container whose Docker
backend had been up 24 hours read 12.8 hours of monotonic time: the eleven
missing hours were nights asleep.

Anything that decides "is this result still fresh" or "may this run again yet"
with monotonic time alone therefore believes far less time has passed than has.
A cached result one hour old at bedtime was, at breakfast, still "47 minutes
old": it was served from cache, no query ran, and the chart sat on yesterday's
numbers while the browser (which counts wall-clock time) knew it was overdue.

The wall clock has the opposite flaw: it can be stepped, by NTP or by the
hypervisor re-syncing after a pause, and a step backwards would make everything
look younger. So neither is trusted alone. Elapsed time is the *larger* of the
two readings, which is right in both failures: a frozen monotonic clock is
overruled by the wall clock, and a wall clock stepped backwards is overruled by
the monotonic one. The only mistake left is a wall clock stepped *forward*, and
the cost of that is one early refresh.
"""

from __future__ import annotations

import time


def elapsed_ms(mono_start: float, wall_start: float) -> float:
    """Milliseconds since a moment noted on both clocks: the larger reading.

    ``mono_start`` is a ``time.monotonic()`` value and ``wall_start`` a
    ``time.time()`` value, both taken at the same instant. Never negative.
    """
    return max(
        (time.monotonic() - mono_start) * 1000,
        (time.time() - wall_start) * 1000,
        0.0,
    )
