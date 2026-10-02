"""Request and response models for a query's charts.

A chart says how to draw a query's result: its type, which columns map to which
axes. Charts render from one already-fetched result, so adding one costs nothing
at the target database. The list of chart types is in ``app/policy/chart_types.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.policy.chart_types import ChartType
from app.policy.flag_rules import FlagOperator, FlagSeverity
from app.types import UtcDatetime

#: ``private`` until somebody asks, ``pending`` while an administrator has not
#: decided, ``published`` once one has (or an administrator published it).
PublishStatus = Literal["private", "pending", "published"]


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
    #: Already resolved against the app-wide default by ``build_chart``, so a
    #: client never has to know what that default is. Optional only so a
    #: payload cached before the field existed still validates.
    surge_threshold_pct: float | None = None
    warnings: list[str] = Field(default_factory=list)


class QueryChartBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    chart_type: ChartType = ChartType.TABLE
    x_field: str | None = Field(default=None, max_length=255)
    y_field: str | None = Field(default=None, max_length=255)
    series_field: str | None = Field(default=None, max_length=255)
    #: Magnitude, so 50 covers a 50% rise and a 50% fall. None follows the
    #: app-wide default. The upper bound is a typo guard, not an opinion: a
    #: threshold above 100000% can never fire on real data and is far more
    #: likely to be a slipped decimal point than an intention.
    surge_threshold_pct: float | None = Field(default=None, gt=0, le=100_000)


class PublishRejectionRead(BaseModel):
    """Why the last request was turned down, kept until the author asks again."""

    reason: str | None
    rejected_at: UtcDatetime
    rejected_by_name: str


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
    surge_threshold_pct: float | None
    #: Visible to every signed-in user. False for every chart until somebody
    #: publishes it; nothing is published by migration.
    is_public: bool
    #: Who published it, which is who may retract it. An admin may always.
    published_by: str | None
    published_at: UtcDatetime | None
    #: Derived from the columns above and the request fields below, never stored.
    publish_status: PublishStatus
    #: When the author asked, while ``publish_status`` is ``pending``.
    publish_requested_at: UtcDatetime | None = None
    #: Set after a rejection, until the author asks again or withdraws.
    publish_rejection: PublishRejectionRead | None = None
    #: The publisher's display name, so a viewer can say whose chart this is.
    published_by_name: str | None = None
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


class PublishRejectRequest(BaseModel):
    """What an administrator says when declining a request."""

    reason: str | None = Field(default=None, max_length=500)


class PublishApproveRequest(BaseModel):
    """What approval is bound to: the definition the administrator was shown."""

    definition_fingerprint: str = Field(min_length=1, max_length=128)


class RequesterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    full_name: str
    email: str


class PublishRequestRead(BaseModel):
    """One waiting request, with enough to know what is being approved."""

    chart: QueryChartRead
    query_id: str
    query_name: str
    connection_id: str
    connection_name: str
    requested_by: RequesterRead
    requested_at: UtcDatetime
    #: Hash of the definition as stored now. Send it back to approve: approval is
    #: bound to the definition that was reviewed.
    definition_fingerprint: str


class DefinitionQueryRead(BaseModel):
    """The query behind a chart, as a reader who may copy it sees it.

    Effective values rather than stored ones: a viewer wants to know the row
    limit and interval the query really runs with, not that the author left a
    field empty.
    """

    id: str
    name: str
    description: str | None
    sql_text: str
    row_limit: int
    poll_interval_ms: int


class DefinitionConditionRead(BaseModel):
    """One condition of a rule. Names the list, never carries its items."""

    model_config = ConfigDict(from_attributes=True)

    column_name: str
    operator: FlagOperator
    value: str | None
    value2: str | None
    list_name: str | None


class DefinitionRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    severity: FlagSeverity
    enabled: bool
    conditions: list[DefinitionConditionRead]


class ChartDefinitionRead(BaseModel):
    """Everything needed to replicate a published chart, and nothing to change it.

    Deliberately without the connection: no host, database, username or id. The
    reader gets the connection's *name* so they know where to point their own copy.
    """

    chart: QueryChartRead
    query: DefinitionQueryRead
    rules: list[DefinitionRuleRead]
    connection_name: str
    owner_name: str | None
    #: True unless the caller is the author or an administrator.
    read_only: bool
    #: Hash of what is shown above and in the chart (SQL, limits, mapping, rules), as
    #: stored now. A list's items are not part of it.
    definition_fingerprint: str
