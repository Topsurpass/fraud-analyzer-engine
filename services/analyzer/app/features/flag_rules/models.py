"""User-defined conditions that mark rows in a saved query's result.

A rule is a named, severity-tagged set of conditions that all have to hold. A
row is flagged when any of a query's enabled rules matches it. Two levels give
both AND and OR without an expression language to parse or secure.

Conditions live in their own table rather than a JSON column on the rule. The
database then enforces the shape -- an operator outside the enum cannot be
stored, a condition cannot outlive its rule -- and a specific condition can be
named in a validation error, which is what makes the editor able to point at
the row the user got wrong.

This module also holds ``FlaggedRow`` (the stored queue of matches) and
``FlagDismissal`` (fingerprints of rows an analyst reviewed); each carries its
original design note as a comment above the class.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, new_id
from app.enums import enum_column
from app.policy.flag_rules import FlagOperator, FlagSeverity

if TYPE_CHECKING:
    from app.features.lists.models import ItemList
    from app.features.queries.models import SavedQuery


class FlagCondition(Base):
    """One comparison against one column of the result set."""

    __tablename__ = "flag_conditions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("flag_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Display and evaluation order within the rule. Contiguous from 0.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    #: A column name in the *result*, not in any table. The engine never knows
    #: the target schema, so this cannot be a foreign key to anything and is
    #: only checked when a result actually arrives.
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[FlagOperator] = mapped_column(
        enum_column(FlagOperator), nullable=False
    )

    #: Both comparands are text regardless of the column's type. The evaluator
    #: decides per cell whether to compare numerically, lexically or as a
    #: boolean, because the same rule can meet a Decimal on Postgres and a
    #: float on SQLite. Storing a typed value would force that decision at
    #: write time, before the result's types are known.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Upper bound for ``between``. Unused by every other operator.
    value2: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The list ``in_list`` / ``not_in_list`` compares against; unused by every
    #: other operator. RESTRICT so the database itself refuses to delete a list
    #: a rule still reads, even if two requests race past the service's check.
    list_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("item_lists.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    rule: Mapped["FlagRule"] = relationship(back_populates="conditions")
    #: selectin: a rule set is read as a whole and evaluated on every poll, so a
    #: lazy load here would cost one statement per list condition. Only the list
    #: row loads; its items stay lazy until the member set is actually needed.
    list: Mapped["ItemList | None"] = relationship(lazy="selectin")

    @property
    def list_name(self) -> str | None:
        return self.list.name if self.list is not None else None


class FlagRule(TimestampMixin, Base):
    __tablename__ = "flag_rules"
    __table_args__ = (
        UniqueConstraint("query_id", "name", name="uq_flag_rules_query_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    severity: Mapped[FlagSeverity] = mapped_column(
        enum_column(FlagSeverity),
        nullable=False,
        default=FlagSeverity.MEDIUM,
        server_default=FlagSeverity.MEDIUM.value,
    )
    #: Turned off rather than deleted, so an analyst can silence a noisy rule
    #: without losing how it was written.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    query: Mapped["SavedQuery"] = relationship(back_populates="flag_rules")
    conditions: Mapped[list[FlagCondition]] = relationship(
        back_populates="rule",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=FlagCondition.position,
    )


# Rows a query's flag rules matched, kept so they can be reviewed later.
#
# Flagged rows used to be recomputed from the result cache on every load, which
# meant they existed only as long as the cache entry and only if somebody had
# recently opened the query. Storing them turns the flagged view into a real
# queue: findings accumulate while nobody is watching, survive a restart, and
# carry when they were first seen.
#
# The trade is explicit. This holds a copy of the matched rows' values, so the
# engine now stores customer data rather than only pointing at it. It is bounded
# to rows that matched a rule the analyst wrote, it cascades away with the query,
# and dismissing a row deletes it. Nothing here is ever written back to the target
# database: those connections are opened read-only and this table is the engine's
# own bookkeeping.
#
# ``row_fingerprint`` is the same hash used by :mod:`app.features.flag_rules.dismissals`,
# so a row identifies the same finding whether it is being stored, matched against
# a dismissal, or looked up after the next run.
#

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


# Flagged rows an analyst has reviewed and does not want to see again.
#
# Flagged rows are not stored. They are recomputed from a query's cached result
# every time the flagged view is opened, which is what keeps the engine from
# holding a second copy of a customer's data. That leaves nothing to attach a
# "dismissed" marker to, so a dismissal records a *fingerprint* of the row's
# contents instead: a hash over the values as they were serialised for the wire.
#
# The consequence is deliberate and worth stating, because it decides whether
# this is useful for review or actively dangerous. If the underlying row changes
# in any way -- a status moves, an amount is corrected -- its fingerprint changes
# with it, so it is no longer the row that was dismissed and it comes back. A
# dismissal says "I looked at this exact row and it is fine", never "stop telling
# me about this account".
#
# Only the hash is stored, never the values, so this table cannot be read back
# into the data it describes.
#

class FlagDismissal(TimestampMixin, Base):
    __tablename__ = "flag_dismissals"
    __table_args__ = (
        # Dismissing the same row twice is a no-op, not an error, and the index
        # is also what makes the per-user, per-query lookup on every flagged-view
        # load a single seek rather than a scan. ``user_id`` is part of the key:
        # a dismissal is personal (see the column).
        UniqueConstraint(
            "query_id", "user_id", "row_fingerprint", name="uq_flag_dismissals_row"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Whose dismissal this is. Personal: a viewer of a published chart who
    #: dismisses a finding clears it for themselves, and the author, an admin and
    #: every other viewer still see it. CASCADE because a dismissal is that
    #: person's own reading state and means nothing without them.
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: sha256 hex of the row's values. Scoped to the query: the same values in
    #: two different queries are two different findings to review.
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
