"""A named, ordered arrangement of saved queries, possibly across connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id

if TYPE_CHECKING:
    from app.models.saved_query import SavedQuery


class DashboardItem(Base):
    """One saved query's place on one dashboard.

    An association row rather than a JSON column of ids, so the database can
    keep the reference honest: deleting a saved query removes it from every
    dashboard through the foreign key, instead of leaving boards pointing at
    something that no longer exists.
    """

    __tablename__ = "dashboard_items"

    dashboard_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("dashboards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Display order within the dashboard. Contiguous from 0 after any write.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    dashboard: Mapped["Dashboard"] = relationship(back_populates="items")
    query: Mapped["SavedQuery"] = relationship(back_populates="dashboard_items")


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
    def query_ids(self) -> list[str]:
        """Query ids in display order, which is what the API exposes."""
        return [item.query_id for item in sorted(self.items, key=lambda i: i.position)]
