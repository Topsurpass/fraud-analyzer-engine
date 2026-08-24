"""Rows a query's flag rules matched, kept so they can be reviewed later.

Flagged rows used to be recomputed from the result cache on every load, which
meant they existed only as long as the cache entry and only if somebody had
recently opened the query. Storing them turns the flagged view into a real
queue: findings accumulate while nobody is watching, survive a restart, and
carry when they were first seen.

The trade is explicit. This holds a copy of the matched rows' values, so the
engine now stores customer data rather than only pointing at it. It is bounded
to rows that matched a rule the analyst wrote, it cascades away with the query,
and dismissing a row deletes it. Nothing here is ever written back to the target
database: those connections are opened read-only and this table is the engine's
own bookkeeping.

``row_fingerprint`` is the same hash used by :mod:`app.services.flag_dismissal_service`,
so a row identifies the same finding whether it is being stored, matched against
a dismissal, or looked up after the next run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import FlagSeverity, enum_column


class FlaggedRow(TimestampMixin, Base):
    __tablename__ = "flagged_rows"
    __table_args__ = (
        # One stored finding per row per query. A re-run updates the existing
        # row rather than appending a duplicate for every poll.
        UniqueConstraint("query_id", "row_fingerprint", name="uq_flagged_rows_row"),
        # The flagged view sorts a connection's findings worst-first; without
        # this it sorts them in memory after reading everything.
        Index("ix_flagged_rows_query_seen", "query_id", "first_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The row exactly as it was serialised for the wire, so the flagged view
    #: can show it without re-running anything. JSON rather than columns: the
    #: engine never knows the target's schema, and the shape differs per query.
    values: Mapped[list[Any]] = mapped_column(JSON, nullable=False)

    #: The headers those values belong under, stored with them rather than
    #: looked up. A finding then renders correctly even after someone edits the
    #: SELECT list, which would otherwise silently relabel every column of
    #: every row flagged before the edit.
    columns: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    #: Ids of the rules that matched, and the highest severity among them.
    #: Denormalised on purpose: rule ids are not stable across a rule-set save,
    #: so a foreign key here would break every time the analyst edited a rule,
    #: and the severity is what the queue sorts by.
    rule_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    rule_names: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    severity: Mapped[FlagSeverity] = mapped_column(
        enum_column(FlagSeverity), nullable=False
    )

    #: When this finding first appeared, and when it was last still matching.
    #: first_seen_at is the one an analyst cares about - "this has been sitting
    #: here for three days" - and it survives every re-run.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
