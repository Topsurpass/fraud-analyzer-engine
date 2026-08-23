"""Execute validated SQL, cap rows, hash the payload, shape the chart block.

Two decisions here are worth knowing about.

**Rows are capped on the fetch side, not by rewriting the SQL.** Wrapping a
user statement as ``SELECT * FROM (<sql>) t LIMIT n`` breaks on MySQL whenever
the inner query has duplicate output column names, and appending ``LIMIT``
textually is fragile against ``UNION``, an existing ``LIMIT``, and window or
CTE forms. Fetching ``row_limit + 1`` rows and stopping is one code path for
every engine and no query shape can defeat it. Server-side work stays bounded
by the statement timeout, which is what actually protects the target database.

**Values are coerced to JSON-safe types once, before hashing.** The bytes that
get hashed are the bytes that get serialised, so ``data_hash`` cannot disagree
with the payload the client received.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.db import target_registry
from app.errors import AppError, ErrorCode, ResultTooLargeError
from app.models import ChartType, Connection, SavedQuery
from app.security.sql_guard import validate_select
from app.services.sizing import approx_json_size


_INF = math.inf


@dataclass(slots=True)
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: int


@dataclass(slots=True)
class RunPayload:
    """The full chart-ready response body for one execution."""

    query_id: str
    executed_at: datetime
    duration_ms: int
    row_count: int
    truncated: bool
    data_hash: str
    columns: list[str]
    rows: list[list[Any]]
    chart: dict = field(default_factory=dict)

    def as_dict(self, poll_interval_ms: int) -> dict:
        """Build the response body without copying the rows.

        Deliberately not ``dataclasses.asdict``. That recurses into every list
        and calls ``copy.deepcopy`` on every leaf: for a 10,000 x 5 result it
        made 50,000 deepcopy calls and a full second copy of the payload, 43 ms
        measured, immediately serialised and discarded. The rows are already
        JSON-safe and are not mutated after this point, so referencing them is
        both correct and free.
        """
        return {
            "query_id": self.query_id,
            "executed_at": self.executed_at,
            "duration_ms": self.duration_ms,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "data_hash": self.data_hash,
            "columns": self.columns,
            "rows": self.rows,
            "chart": self.chart,
            "poll_interval_ms": poll_interval_ms,
        }


# ---------------------------------------------------------------------------
# JSON coercion and hashing
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Convert a driver value into something ``json.dumps`` accepts.

    Every branch is deterministic, which is what lets the same result hash
    identically across runs and across processes.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # NaN and +/-Infinity are real values in a customer's data (a division
        # by zero in a stored aggregate, a 1e400 literal in SQLite) but they
        # are not JSON. canonical_hash uses allow_nan=False, so letting one
        # through raised ValueError, which is not an AppError -- the request
        # became an opaque 500 and _execute_and_log's `except AppError` missed
        # it, so no execution-log row was written either. The user saw a 500
        # on data they could see was there, and /logs showed nothing.
        #
        # null is the honest wire representation: no chart can plot NaN, and
        # the alternative of a "NaN" string would silently change the column's
        # type partway down a series.
        if value != value or value in (_INF, -_INF):
            return None
        return value
    if isinstance(value, Decimal):
        # str, not float: a Decimal that cannot be represented exactly in
        # binary floating point would otherwise hash differently per platform.
        # This also carries Decimal('NaN') and Decimal('Infinity') safely, as
        # the strings "NaN" and "Infinity" rather than as non-JSON floats.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dt_time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)


def canonical_hash(columns: list[str], rows: list[list[Any]]) -> str:
    """Stable sha256 over the exact payload the client will receive."""
    canonical = json.dumps(
        {"columns": columns, "rows": rows},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def resolve_row_limit(requested: int | None) -> int:
    """Clamp a requested row limit, rejecting anything past the hard ceiling."""
    settings = get_settings()
    if requested is None:
        return settings.default_row_limit
    if requested < 1:
        raise AppError(
            ErrorCode.ROW_LIMIT_EXCEEDED, "row_limit must be at least 1.", {"row_limit": requested}
        )
    if requested > settings.max_row_limit:
        raise AppError(
            ErrorCode.ROW_LIMIT_EXCEEDED,
            f"row_limit {requested} exceeds the maximum of {settings.max_row_limit}.",
            {"row_limit": requested, "max_row_limit": settings.max_row_limit},
        )
    return requested


def execute_sql(
    conn: Connection,
    sql: str,
    row_limit: int,
    timeout_s: float | None = None,
) -> ExecutionResult:
    """Validate, execute, and return at most ``row_limit`` rows.

    The statement sent to the database is the guard's sanitised output, never
    the caller's original string.
    """
    safe_sql = validate_select(sql)
    started = time.perf_counter()

    budget = get_settings().max_result_bytes

    with target_registry.read_only_connection(conn, timeout_s=timeout_s) as sa_conn:
        result = sa_conn.execute(text(safe_sql))
        columns = list(result.keys())
        # One extra row is the truncation signal: getting it back means the
        # result set was larger than the cap.
        fetched = result.fetchmany(row_limit + 1)
        truncated = len(fetched) > row_limit
        if truncated:
            fetched = fetched[:row_limit]

        # row_limit bounds how many rows come back but says nothing about how
        # wide they are. A single SELECT repeat('x', 1000000000) is one row and
        # a gigabyte, and it would otherwise be coerced, hashed, cached and
        # serialised in full. Checking as rows are built stops that at the
        # first row past the budget rather than after the damage is done.
        rows: list[list[Any]] = []
        used = 0
        for row in fetched:
            coerced = [to_jsonable(value) for value in row]
            used += sum(approx_json_size(value) for value in coerced)
            if used > budget:
                result.close()
                raise ResultTooLargeError(
                    f"The result passed the {budget} byte limit after "
                    f"{len(rows)} rows. Narrow the columns or lower the row "
                    f"limit.",
                    {"max_result_bytes": budget, "rows_returned": len(rows)},
                )
            rows.append(coerced)
        result.close()

    duration_ms = int((time.perf_counter() - started) * 1000)
    return ExecutionResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Chart shaping
# ---------------------------------------------------------------------------

#: Fields each chart type actually consumes, used to warn on a bad mapping.
_REQUIRED_FIELDS: dict[ChartType, tuple[str, ...]] = {
    ChartType.LINE: ("x_field", "y_field"),
    ChartType.BAR: ("x_field", "y_field"),
    ChartType.PIE: ("x_field", "y_field"),
    ChartType.NUMBER: ("y_field",),
    ChartType.TABLE: (),
}


def build_chart(query: SavedQuery, columns: list[str]) -> dict:
    """Echo the chart mapping, warning about fields the result does not contain.

    A bad mapping is a warning rather than an error: the rows are still useful,
    and failing the whole run would hide data the user can see is there.
    """
    warnings: list[str] = []
    available = set(columns)

    for field_name in ("x_field", "y_field", "series_field"):
        value = getattr(query, field_name)
        if value and value not in available:
            warnings.append(
                f"{field_name} {value!r} is not in the result columns {sorted(available)}"
            )

    for field_name in _REQUIRED_FIELDS.get(query.chart_type, ()):
        if not getattr(query, field_name):
            warnings.append(
                f"chart_type {query.chart_type.value!r} needs {field_name} to be set"
            )

    return {
        "type": query.chart_type.value,
        "x_field": query.x_field,
        "y_field": query.y_field,
        "series_field": query.series_field,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Saved-query orchestration
# ---------------------------------------------------------------------------


def poll_interval_for(query: SavedQuery) -> int:
    """Per-query interval if set, otherwise the global default."""
    return query.poll_interval_ms or get_settings().poll_interval_ms


def run_saved_query(query: SavedQuery, conn: Connection) -> RunPayload:
    """Execute a saved query and build its full chart-ready payload."""
    from app.models import utcnow

    result = execute_sql(conn, query.sql_text, row_limit=query.row_limit)
    return RunPayload(
        query_id=query.id,
        executed_at=utcnow(),
        duration_ms=result.duration_ms,
        row_count=result.row_count,
        truncated=result.truncated,
        data_hash=canonical_hash(result.columns, result.rows),
        columns=result.columns,
        rows=result.rows,
        chart=build_chart(query, result.columns),
    )


def dry_run(conn: Connection, sql: str) -> None:
    """Prove a statement actually runs against this connection before saving.

    Validation catches SQL that is unsafe. A dry run catches SQL that is safe
    but wrong for this database: a misspelled table, a column that does not
    exist, a permission the role does not have. Saving a query that can never
    execute would just move the failure to the dashboard.
    """
    execute_sql(conn, sql, row_limit=1)
