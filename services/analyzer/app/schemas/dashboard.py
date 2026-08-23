"""Request and response models for dashboards."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.types import UtcDatetime


class DashboardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: Saved-query ids in display order. May span connections.
    query_ids: list[str] = Field(default_factory=list)


class DashboardUpdate(BaseModel):
    """Every field optional. Omitted fields are left untouched.

    Passing ``query_ids`` replaces the whole arrangement rather than merging
    into it: a dashboard is an ordered list, and a partial merge has no
    well-defined meaning for order.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    query_ids: list[str] | None = None


class DashboardRead(BaseModel):
    id: str
    name: str
    query_ids: list[str]
    created_at: UtcDatetime
    updated_at: UtcDatetime
