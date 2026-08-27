"""A named, SELECT-only query bound to one connection.

Chart configuration lives in :mod:`app.models.query_chart`, not here. When the
two were the same object, three views of one result meant three saved queries,
three executions of identical SQL against the target, and three cache entries -
the result cache is keyed by query id. A query now owns *what to fetch and how
often*; a chart owns *how to draw it*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id

if TYPE_CHECKING:
    from app.models.connection import Connection
    from app.models.dashboard import DashboardItem
    from app.models.execution_log import QueryExecutionLog
    from app.models.flag_rule import FlagRule
    from app.models.query_chart import QueryChart

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
