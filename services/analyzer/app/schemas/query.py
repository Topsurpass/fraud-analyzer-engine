"""Request and response models for saved queries and their results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ChartType
from app.schemas.types import UtcDatetime


class SavedQueryBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    sql_text: str = Field(min_length=1)
    table_hint: str | None = Field(default=None, max_length=255)
    chart_type: ChartType = ChartType.TABLE
    x_field: str | None = Field(default=None, max_length=255)
    y_field: str | None = Field(default=None, max_length=255)
    series_field: str | None = Field(default=None, max_length=255)
    row_limit: int | None = Field(default=None, ge=1)
    poll_interval_ms: int | None = Field(default=None, ge=100)


class SavedQueryCreate(SavedQueryBase):
    pass


class SavedQueryUpdate(BaseModel):
    """Every field optional. Omitted fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    sql_text: str | None = Field(default=None, min_length=1)
    table_hint: str | None = Field(default=None, max_length=255)
    chart_type: ChartType | None = None
    x_field: str | None = Field(default=None, max_length=255)
    y_field: str | None = Field(default=None, max_length=255)
    series_field: str | None = Field(default=None, max_length=255)
    row_limit: int | None = Field(default=None, ge=1)
    poll_interval_ms: int | None = Field(default=None, ge=100)


class SavedQueryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    connection_id: str
    name: str
    description: str | None
    sql_text: str
    table_hint: str | None
    chart_type: ChartType
    x_field: str | None
    y_field: str | None
    series_field: str | None
    row_limit: int
    poll_interval_ms: int | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ChartSpec(BaseModel):
    type: ChartType
    x_field: str | None = None
    y_field: str | None = None
    series_field: str | None = None
    warnings: list[str] = Field(default_factory=list)


class RunResponse(BaseModel):
    query_id: str
    executed_at: UtcDatetime
    duration_ms: int
    row_count: int
    truncated: bool
    data_hash: str
    columns: list[str]
    rows: list[list[Any]]
    chart: ChartSpec
    poll_interval_ms: int


class PollUnchanged(BaseModel):
    """The cheap path: nothing moved since ``since_hash``."""

    query_id: str
    changed: bool = False
    data_hash: str
    poll_interval_ms: int
    from_cache: bool


class PollChanged(RunResponse):
    changed: bool = True
    from_cache: bool = False


class PreviewRequest(BaseModel):
    sql_text: str = Field(min_length=1)
    row_limit: int | None = Field(default=None, ge=1)


class PreviewResponse(BaseModel):
    connection_id: str
    executed_at: UtcDatetime
    duration_ms: int
    row_count: int
    truncated: bool
    columns: list[str]
    rows: list[list[Any]]


class ExecutionLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    query_id: str
    executed_at: UtcDatetime
    row_count: int | None
    duration_ms: int | None
    success: bool
    error_code: str | None
    error_message: str | None


class BatchPollItem(BaseModel):
    """One query's place in a batch poll request."""

    query_id: str
    since_hash: str | None = None


class BatchPollRequest(BaseModel):
    queries: list[BatchPollItem] = Field(min_length=1, max_length=100)
    force: bool = False


class BatchPollFailure(BaseModel):
    """One query's failure, carried inside an otherwise successful batch.

    A batch must not fail wholesale because one card's SQL is broken: the
    other eleven cards on the board are fine and should still render.
    """

    query_id: str
    ok: bool = False
    error_code: str
    message: str
    detail: dict | None = None


class BatchPollResponse(BaseModel):
    results: list[dict]
