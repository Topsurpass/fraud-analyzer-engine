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
    # Scoped to the owner, not global. A unique ``name`` alone made every
    # board title a shared namespace: the second analyst to reach for "Fraud
    # Review" got a 409 about a board that is not in their listing and that
    # they are not allowed to see, which is the same cross-analyst existence
    # leak the 404-not-403 rule closes on the read path. Within one person's
    # own boards a duplicate name is still refused.
    #
    # NULL owner_ids do not collide with each other, because SQL treats NULLs
    # in a unique constraint as distinct. That only reaches rows predating
    # accounts - every create sets an owner - and duplicate names among
    # unowned legacy rows are the lesser problem.
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_dashboards_owner_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Who created this. Nullable because rows predating accounts have no
    #: owner, and because an owner is never deleted so the column never has to
    #: be cleared. An unowned row is visible to administrators only.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

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
