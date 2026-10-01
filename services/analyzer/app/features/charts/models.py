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

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id
from app.enums import enum_column
from app.policy.chart_types import ChartType

if TYPE_CHECKING:
    from app.features.dashboards.models import DashboardItem
    from app.features.queries.models import SavedQuery
    from app.features.users.models import User


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

    #: Visible to every signed-in user, not just the owner.
    #:
    #: Publishing is how the private-work model shares anything at all. An
    #: analyst may publish a chart they own; an admin may publish anyone's.
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    #: Who published it, which decides who may unpublish it. An analyst can
    #: retract their own publication; a chart an admin published stays the
    #: admin's to retract, so an admin keeps a genuine freeze over anyone's
    #: work.
    published_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: An analyst's publish is a request, not a publication: it waits here for
    #: an administrator. Set while the chart is pending, cleared by approval,
    #: rejection or withdrawal. Whether a chart is pending is derived from this
    #: (see ``publish_status``) rather than stored a second time.
    publish_requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    publish_requested_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True
    )

    #: The last rejection, kept until the author asks again or withdraws, so the
    #: card can say why instead of the request silently vanishing.
    publish_rejected_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    publish_rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    publish_rejected_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    query: Mapped["SavedQuery"] = relationship(back_populates="charts")
    publisher: Mapped["User | None"] = relationship(foreign_keys=[published_by], viewonly=True)
    rejector: Mapped["User | None"] = relationship(
        foreign_keys=[publish_rejected_by], viewonly=True
    )
    requester: Mapped["User | None"] = relationship(
        foreign_keys=[publish_requested_by], viewonly=True
    )
    #: Deleting a chart takes it off every dashboard that showed it.
    dashboard_items: Mapped[list["DashboardItem"]] = relationship(
        back_populates="chart",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def publish_status(self) -> str:
        """``private``, ``pending`` or ``published``, derived from the columns.

        Derived rather than stored: a stored status would be a second source of
        truth, and the day it disagreed with ``is_public`` the symptom would be
        a chart some people can see and the API says is private.
        """
        if self.is_public:
            return "published"
        if self.publish_requested_at is not None:
            return "pending"
        return "private"

    @property
    def published_by_name(self) -> str | None:
        """Who published it, as a name a viewer can read."""
        return self.publisher.full_name if self.publisher is not None else None

    @property
    def publish_rejection(self) -> dict | None:
        """The standing rejection notice, or None."""
        if self.publish_rejected_at is None:
            return None
        return {
            "reason": self.publish_rejected_reason,
            "rejected_at": self.publish_rejected_at,
            "rejected_by_name": self.rejector.full_name if self.rejector is not None else "",
        }
