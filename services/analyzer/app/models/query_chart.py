"""One way of drawing a saved query's result.

Chart configuration used to live on ``SavedQuery`` itself, which made "a query"
and "a chart" the same object. Wanting the same data as a line, a bar and a
table meant saving the query three times, and the engine then executed
identical SQL three times against the target and cached three copies of the
identical result -- because the result cache is keyed by query id.

Separating them means the SQL runs once. The query owns what to fetch and how
often; each chart owns how to draw it. Everything a chart carries is a mapping
onto columns the result already contains, so adding one costs nothing at the
database.

A chart naming a column the result does not contain is a warning at build time,
never an error: the same treatment ``build_chart`` has always given a bad field,
because the rows are still worth showing and the usual cause is editing the
SELECT list after configuring the chart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import ChartType, enum_column

if TYPE_CHECKING:
    from app.models.dashboard import DashboardItem
    from app.models.saved_query import SavedQuery


class QueryChart(TimestampMixin, Base):
    __tablename__ = "query_charts"
    __table_args__ = (
        # Names are how a person tells two charts of the same query apart, and
        # how a dashboard describes what it is showing.
        UniqueConstraint("query_id", "name", name="uq_query_charts_query_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Display order within the query. Contiguous from 0 after any write.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    chart_type: Mapped[ChartType] = mapped_column(
        enum_column(ChartType),
        nullable=False,
        default=ChartType.TABLE,
        server_default=ChartType.TABLE.value,
    )
    #: Column names in the *result*, not in any table. The engine never knows
    #: the target schema, so these cannot be foreign keys to anything and are
    #: only checked when a result actually arrives.
    x_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    y_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    series_field: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Percent change past which a movement is called out for investigation,
    #: as a magnitude: 50 means "flag a rise of 50% or more, and a fall of 50%
    #: or more". A percentage rather than an absolute figure because terminals
    #: carry wildly different volume, and an absolute jump that is alarming on
    #: a quiet terminal is noise on a busy one.
    #:
    #: NULL means "use the app-wide default" and is not the same as storing
    #: that default: an unset chart follows the default when it changes.
    surge_threshold_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    query: Mapped["SavedQuery"] = relationship(back_populates="charts")
    #: Deleting a chart takes it off every dashboard that showed it.
    dashboard_items: Mapped[list["DashboardItem"]] = relationship(
        back_populates="chart",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
