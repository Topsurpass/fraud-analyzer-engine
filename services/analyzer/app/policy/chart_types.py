"""Chart types a saved query may be drawn as.

EDIT THIS FILE to add or remove a chart type. The value is what the API accepts
and what the database stores. A new member is stored as a string, so it needs no
migration by itself; the frontend must learn to draw it, and the new type needs
an entry in ``REQUIRED_FIELDS`` at the bottom (``tests/test_policy.py`` fails
without one). To retire one, check no stored ``query_charts.chart_type`` row
still uses it first: an unknown stored value fails when the row is read. Old
migrations (0005, 0008, 0009) import these enums, so removing a member also
breaks ``alembic upgrade`` from an empty database until those migrations are
changed.
"""

from __future__ import annotations

from enum import StrEnum


class ChartType(StrEnum):
    LINE = "line"
    BAR = "bar"
    PIE = "pie"
    NUMBER = "number"
    TABLE = "table"

    #: The same measure over two consecutive windows, drawn on top of each
    #: other. An analyst reads the *gap* rather than the level: a terminal
    #: whose current line has pulled away from the previous one is doing
    #: something it was not doing an hour ago, which is the question a fraud
    #: queue actually asks.
    COMPARE = "compare"

    #: The same two windows as ``COMPARE``, but totalled per category instead
    #: of plotted over time. ``COMPARE`` answers "did this move"; this answers
    #: "which terminal moved", which is the question that names a suspect.
    MOVERS = "movers"

    #: One ``COMPARE`` panel per category, laid out as small multiples.
    #: ``COMPARE`` gives the shape for everything at once and ``MOVERS`` gives
    #: two totals per category; this gives the shape *per* category, which is
    #: the only one of the three that shows a terminal changing its rhythm
    #: rather than just its level.
    COMPARE_GRID = "compare_grid"

    #: A category against a time bucket, coloured by intensity. Scanning fifty
    #: terminals across twenty-four hours as fifty line charts is impossible;
    #: as one grid the odd row or the odd hour is immediate.
    HEATMAP = "heatmap"

    #: A bar chart whose ``series_field`` splits each bar into stacked
    #: segments instead of placing them side by side. The total and its
    #: composition in one mark: "how much, and made of what". With no
    #: ``series_field`` it is an ordinary bar chart.
    STACKED_BAR = "stacked_bar"

    #: Two different measures over one category axis, each against its own
    #: y axis: a count beside a rate, which on one shared axis would flatten
    #: the rate into the floor. ``y_field`` is the left-axis column and
    #: ``series_field`` names the right-axis column (wide form, one row per
    #: x), because a chart spec has a single ``y_field``.
    BIAXIAL_BAR = "biaxial_bar"


#: Fields each chart type actually consumes, used to warn on a bad mapping.
REQUIRED_FIELDS: dict[ChartType, tuple[str, ...]] = {
    ChartType.LINE: ("x_field", "y_field"),
    ChartType.BAR: ("x_field", "y_field"),
    ChartType.PIE: ("x_field", "y_field"),
    # Two windows of one measure: the bucket to split on, and what to plot.
    ChartType.COMPARE: ("x_field", "y_field"),
    # The bucket to split the windows on, the measure to total, and the
    # category to total it per.
    ChartType.MOVERS: ("x_field", "y_field", "series_field"),
    # A panel per category: the bucket, the measure, and what to split on.
    ChartType.COMPARE_GRID: ("x_field", "y_field", "series_field"),
    # A row per category, a column per bucket, coloured by the value.
    ChartType.HEATMAP: ("x_field", "y_field", "series_field"),
    # The series split is optional (none is a plain bar), so only the axes
    # are required.
    ChartType.STACKED_BAR: ("x_field", "y_field"),
    # Left measure in y_field, right measure in series_field.
    ChartType.BIAXIAL_BAR: ("x_field", "y_field", "series_field"),
    ChartType.NUMBER: ("y_field",),
    ChartType.TABLE: (),
}
