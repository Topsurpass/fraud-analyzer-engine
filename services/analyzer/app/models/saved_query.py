"""A named, SELECT-only query bound to one connection, plus its chart mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import ChartType, enum_column

if TYPE_CHECKING:
    from app.models.connection import Connection
    from app.models.dashboard import DashboardItem
    from app.models.execution_log import QueryExecutionLog
    from app.models.flag_rule import FlagRule

DEFAULT_ROW_LIMIT = 1000


class SavedQuery(TimestampMixin, Base):
    __tablename__ = "saved_queries"
    __table_args__ = (
        UniqueConstraint("connection_id", "name", name="uq_saved_queries_conn_name"),
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

    chart_type: Mapped[ChartType] = mapped_column(
        enum_column(ChartType),
        nullable=False,
        default=ChartType.TABLE,
        server_default=ChartType.TABLE.value,
    )
    x_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    y_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    series_field: Mapped[str | None] = mapped_column(String(255), nullable=True)

    row_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_ROW_LIMIT, server_default="1000"
    )
    poll_interval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    #: Deleting a query takes it off every dashboard that showed it.
    dashboard_items: Mapped[list["DashboardItem"]] = relationship(
        back_populates="query",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
