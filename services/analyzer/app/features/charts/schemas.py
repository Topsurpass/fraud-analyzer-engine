"""Request and response models for a query's charts.

A chart says how to draw a query's result: its type, which columns map to which
axes. Charts render from one already-fetched result, so adding one costs nothing
at the target database. The list of chart types is in ``app/policy/chart_types.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.policy.chart_types import ChartType
from app.types import UtcDatetime


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
