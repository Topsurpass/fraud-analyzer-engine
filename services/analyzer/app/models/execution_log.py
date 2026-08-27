"""One row per execution attempt, so the UI can show 'last run' and failures."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id, utcnow

if TYPE_CHECKING:
    from app.models.saved_query import SavedQuery


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
