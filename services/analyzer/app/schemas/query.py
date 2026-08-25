"""Request and response models for saved queries and their results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ChartType
from app.schemas.flag_rule import FlagOutcomeRead, FlagRuleBase
from app.schemas.types import UtcDatetime


class SavedQueryBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    sql_text: str = Field(min_length=1)
    table_hint: str | None = Field(default=None, max_length=255)
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
    row_limit: int
    poll_interval_ms: int | None
    #: Every way this query's result can be drawn, in display order.
    #:
    #: Included here rather than fetched separately: a connection page renders
    #: a card per chart, and a request per query just to learn what to draw
    #: would be the per-card cost the query/chart split exists to remove.
    charts: list["QueryChartRead"] = Field(default_factory=list)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ChartSpec(BaseModel):
    """One chart's mapping, as echoed back on a run.

    Carries its own id and name because a payload now holds several: without
    them the client cannot tell which chart a spec belongs to, and a dashboard
    placing one of three has nothing to match on.
    """

    id: str
    name: str
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
    #: Every chart on the query, against the columns this run returned. One
    #: payload serves them all, which is what lets three views of a result cost
    #: one execution and one poll.
    charts: list[ChartSpec] = Field(default_factory=list)
    flags: FlagOutcomeRead = Field(default_factory=FlagOutcomeRead)
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
    #: Rules to try against the preview rows without saving anything. This is
    #: what lets the editor answer "would this rule catch anything?" before the
    #: query exists, which is the difference between writing a rule and
    #: guessing at one.
    flag_rules: list[FlagRuleBase] = Field(default_factory=list, max_length=50)


class PreviewResponse(BaseModel):
    connection_id: str
    executed_at: UtcDatetime
    duration_ms: int
    row_count: int
    truncated: bool
    columns: list[str]
    rows: list[list[Any]]
    flags: FlagOutcomeRead = Field(default_factory=FlagOutcomeRead)


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


class QueryChartBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    chart_type: ChartType = ChartType.TABLE
    x_field: str | None = Field(default=None, max_length=255)
    y_field: str | None = Field(default=None, max_length=255)
    series_field: str | None = Field(default=None, max_length=255)


class QueryChartRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    query_id: str
    name: str
    position: int
    chart_type: ChartType
    x_field: str | None
    y_field: str | None
    series_field: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class QueryChartSetUpdate(BaseModel):
    """Whole-set replace, matching the flag-rule editor's shape.

    The editor edits every chart of a query at once, so position is simply the
    index in this list and no reorder endpoint has to exist. The cap is an
    upper bound rather than an opinion: every chart here renders from one
    already-fetched result, so the cost is layout rather than database load.
    """

    charts: list[QueryChartBase] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _names_are_distinct(self) -> "QueryChartSetUpdate":
        seen: set[str] = set()
        for chart in self.charts:
            key = chart.name.strip().lower()
            if key in seen:
                raise ValueError(f"duplicate chart name {chart.name!r}")
            seen.add(key)
        return self


class QueryChartSetRead(BaseModel):
    query_id: str
    charts: list[QueryChartRead]
