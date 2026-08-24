"""User-defined conditions that mark rows in a saved query's result.

A rule is a named, severity-tagged set of conditions that all have to hold. A
row is flagged when any of a query's enabled rules matches it. Two levels give
both AND and OR without an expression language to parse or secure.

Conditions live in their own table rather than a JSON column on the rule. The
database then enforces the shape -- an operator outside the enum cannot be
stored, a condition cannot outlive its rule -- and a specific condition can be
named in a validation error, which is what makes the editor able to point at
the row the user got wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import FlagOperator, FlagSeverity, enum_column

if TYPE_CHECKING:
    from app.models.saved_query import SavedQuery


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

    rule: Mapped["FlagRule"] = relationship(back_populates="conditions")


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
