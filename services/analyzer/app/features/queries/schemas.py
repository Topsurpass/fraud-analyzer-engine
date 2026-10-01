"""Request and response models for saved queries and their results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.features.charts.schemas import ChartSpec, QueryChartRead
from app.features.flag_rules.schemas import FlagOutcomeRead, FlagRuleBase
from app.types import UtcDatetime


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
    #: When the result being confirmed was produced. A run that returns the same
    #: rows leaves the hash alone, so without this a client that only ever hears
    #: "unchanged" cannot tell when the query last actually ran, and so cannot
    #: line its next poll up with the moment the cached result goes stale.
    executed_at: datetime | None = None


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
    #: Who caused this run. None for a scheduled or background refresh - see
    #: the identical note on QueryExecutionLog.user_id.
    user_id: str | None = None


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


