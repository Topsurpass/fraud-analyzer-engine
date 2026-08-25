"""A named, ordered arrangement of saved queries, possibly across connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id

if TYPE_CHECKING:
    from app.models.query_chart import QueryChart


class DashboardItem(Base):
    """One chart's place on one dashboard.

    An association row rather than a JSON column of ids, so the database can
    keep the reference honest: deleting a chart removes it from every dashboard
    through the foreign key, instead of leaving boards pointing at something
    that no longer exists.

    A *chart* rather than a query, so one query's result can appear on a board
    twice - as a trend line and as the rows behind it - while the SQL runs once.
    """

    __tablename__ = "dashboard_items"

    dashboard_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("dashboards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chart_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("query_charts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Display order within the dashboard. Contiguous from 0 after any write.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    dashboard: Mapped["Dashboard"] = relationship(back_populates="items")
    chart: Mapped["QueryChart"] = relationship(back_populates="dashboard_items")


class Dashboard(TimestampMixin, Base):
    __tablename__ = "dashboards"
    __table_args__ = (UniqueConstraint("name", name="uq_dashboards_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    items: Mapped[list[DashboardItem]] = relationship(
        back_populates="dashboard",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=DashboardItem.position,
    )

    @property
    def chart_ids(self) -> list[str]:
        """Chart ids in display order, which is what the API exposes."""
        return [item.chart_id for item in sorted(self.items, key=lambda i: i.position)]
