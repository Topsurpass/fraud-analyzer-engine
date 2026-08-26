"""Declarative base and shared column mixins for the app-state database."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """``DateTime(timezone=True)`` that stays timezone-aware on SQLite.

    Postgres's ``timestamptz`` round-trips tzinfo faithfully, but SQLite has no
    timezone-aware storage: the driver formats a ``datetime`` to text and hands
    back a *naive* one on the next real read. That "next real read" is not a
    corner case, it is every request in production - a fresh ``Session`` per
    request means the row it loads is never resident in that session's
    identity map, so the value comes back through SQLite's text round-trip
    rather than surviving in Python. Comparing that naive result against an
    aware ``datetime.now(timezone.utc)`` raises ``TypeError`` on SQLite while
    working by accident on Postgres, and the two backends silently disagreeing
    is exactly the kind of thing a local dev environment (SQLite) will never
    catch before it reaches production (Postgres) - or, here, before it reaches
    the test suite, which runs on SQLite for speed and would otherwise pass
    locally and break for real.

    Every column that uses this is written through :func:`utcnow`, so a naive
    value read back is always UTC, and reattaching that tzinfo on load is
    correct rather than a guess.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
