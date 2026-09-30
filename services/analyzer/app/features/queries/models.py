"""A named, SELECT-only query bound to one connection.

Chart configuration lives in :mod:`app.features.charts.models`, not here. When the
two were the same object, three views of one result meant three saved queries,
three executions of identical SQL against the target, and three cache entries -
the result cache is keyed by query id. A query now owns *what to fetch and how
often*; a chart owns *how to draw it*.

This module also holds ``QueryExecutionLog``: one row per execution attempt,
so the UI can show 'last run' and failures.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_id, utcnow

if TYPE_CHECKING:
    from app.features.charts.models import QueryChart
    from app.features.connections.models import Connection
    from app.features.dashboards.models import DashboardItem
    from app.features.flag_rules.models import FlagRule


DEFAULT_ROW_LIMIT = 1000


class SavedQuery(TimestampMixin, Base):
    __tablename__ = "saved_queries"
    # The owner is part of the key. Connections are shared by design, so a
    # unique ``(connection_id, name)`` turned every query name on a shared
    # database into a global namespace: one analyst naming a query told the
    # next one, through a 409, that somebody else's query by that name exists
    # on a connection they both use - about work they cannot see. The
    # connection stays in the key because two connections are two databases
    # and a name reused across them was never a collision. See the note on
    # ``Dashboard.__table_args__`` about NULL owners.
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "owner_id", "name", name="uq_saved_queries_conn_owner_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    table_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)

    row_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_ROW_LIMIT, server_default="1000"
    )
    poll_interval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Who created this. Nullable because rows predating accounts have no
    #: owner, and because an owner is never deleted so the column never has to
    #: be cleared. An unowned row is visible to administrators only.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    connection: Mapped["Connection"] = relationship(back_populates="queries")
    execution_logs: Mapped[list["QueryExecutionLog"]] = relationship(
        back_populates="query",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    #: Ordered by position so the editor round-trips a rule set unchanged.
    flag_rules: Mapped[list["FlagRule"]] = relationship(
        back_populates="query",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="FlagRule.position",
    )
    #: Ways of drawing this query's result. Ordered so the editor and every
    #: dashboard agree on which one is "first".
    charts: Mapped[list["QueryChart"]] = relationship(
        back_populates="query",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="QueryChart.position",
    )


# One row per execution attempt, so the UI can show 'last run' and failures.

class QueryExecutionLog(Base):
    __tablename__ = "query_execution_logs"

    # recent_logs() filters on query_id and orders by executed_at DESC with a
    # LIMIT. The single-column indexes below cannot serve that together: the
    # planner scans one and then sorts the whole match set. This composite
    # answers it straight from the index and stops at the limit.
    __table_args__ = (
        Index(
            "ix_logs_query_executed",
            "query_id",
            text("executed_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    #: The saved query that ran. Null for an ad-hoc preview, which executes
    #: analyst-authored SQL against a customer database without saving it and
    #: so has no query row to point at - see ``connection_id`` below.
    query_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    #: Which database this ran against, recorded only when ``query_id`` is
    #: null. A saved query already names its connection, and duplicating it
    #: would be a second source of truth that could disagree; a preview names
    #: nothing else, and an execution-log row that cannot say which customer
    #: database was touched does not answer the question the log exists for.
    connection_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Who caused this run. Null only for the scheduler, which runs on a timer
    #: with nobody behind it. Every path a person can trigger - a run, a poll,
    #: a preview, a flagged refresh, and the stale-cache refresh a poll starts
    #: behind itself - carries the caller.
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    query: Mapped["SavedQuery | None"] = relationship(back_populates="execution_logs")
