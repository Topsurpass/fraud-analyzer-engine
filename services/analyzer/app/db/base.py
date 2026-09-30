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

    Used by :class:`TimestampMixin` (so ``created_at``/``updated_at`` carry it
    on every model) and directly by ``UserSession`` and the ``User`` lockout
    columns. This is deliberately the type for every timestamp in the schema,
    not just the ones some past bug happened to touch: the alternative is a
    codebase where "does this column compare safely against ``utcnow()``"
    depends on which columns a previous author's bug report reached, and the
    next `if some_model.some_timestamp < utcnow()` reintroduces the exact
    failure this type exists to close - on SQLite only, silent on Postgres,
    which is the worst kind of latent bug to leave lying around.
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
    #: UTCDateTime, not a bare DateTime(timezone=True): every timestamp
    #: column in this schema goes through this mixin or is written the same
    #: way, so this is the one place that decides whether "a datetime loaded
    #: from the database compares cleanly against utcnow()" holds everywhere
    #: or only on the columns some later bug happened to force it onto.
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
