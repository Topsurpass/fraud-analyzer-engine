"""Request and response models for dashboards."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.query import QueryChartRead
from app.schemas.types import UtcDatetime


class DashboardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: Chart ids in display order. May span connections.
    chart_ids: list[str] = Field(default_factory=list)


class DashboardUpdate(BaseModel):
    """Every field optional. Omitted fields are left untouched.

    Passing ``chart_ids`` replaces the whole arrangement rather than merging
    into it: a dashboard is an ordered list, and a partial merge has no
    well-defined meaning for order.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    chart_ids: list[str] | None = None


class DashboardRead(BaseModel):
    id: str
    name: str
    chart_ids: list[str]
    #: The placed charts themselves, in the same order.
    #:
    #: Resolved here rather than left to the client. A board holds chart ids,
    #: and only the chart knows which query it draws - without this every page
    #: load would be a request per card just to learn what to poll, which is
    #: the per-card cost the query/chart split exists to remove.
    charts: list[QueryChartRead] = Field(default_factory=list)
    #: Who built this board. Null for boards that predate accounts.
    #:
    #: Exposed so a viewer can tell their own board from somebody else's. An
    #: admin sees every board, and a board they are merely inspecting should
    #: show what its owner actually placed rather than being dressed up with
    #: the viewer's own shared cards.
    owner_id: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
