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
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=False,
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

    query: Mapped["SavedQuery"] = relationship(back_populates="execution_logs")
