"""Shared field types for response models.

``UtcDatetime`` exists because SQLite has no timezone-aware column type. The
models declare ``DateTime(timezone=True)``, which PostgreSQL honours and SQLite
silently ignores, so the same field came back aware on Neon and naive on
SQLite. Pydantic serialises those differently:

    "2026-08-23T09:06:30.979602Z"   <- app-state on Postgres, computed in-process
    "2026-08-23T09:06:30.979602"    <- the same field, app-state on SQLite

The second form is the damaging one. ``new Date("2026-08-23T09:06:30.979602")``
in JavaScript parses as *local* time, so every "created" and "last run"
timestamp in the dashboard was silently offset by the viewer's UTC offset --
and only on one of the two supported backends, which is the kind of bug that
never reproduces where anyone is looking for it.

Everything this service stores is written by ``models.utcnow``, so a naive
value read back is UTC that lost its label. Reattaching it is a correction,
not a guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator


def _assume_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


#: A datetime guaranteed to carry UTC on the wire, whatever the backend stored.
UtcDatetime = Annotated[datetime, AfterValidator(_assume_utc)]
