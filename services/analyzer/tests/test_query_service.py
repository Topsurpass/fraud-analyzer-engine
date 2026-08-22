"""Execution, fetch-side row capping, deterministic hashing, chart shaping."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.errors import AppError, ErrorCode, SqlValidationError
from app.models import ChartType, Connection, DbType, SavedQuery
from app.services.query_service import (
    build_chart,
    canonical_hash,
    execute_sql,
    resolve_row_limit,
    to_jsonable,
)


def sqlite_conn(path: str) -> Connection:
    c = Connection(name="t", db_type=DbType.SQLITE, sqlite_path=path)
    c.id = "conn-1"
    return c


# ---------------------------------------------------------------------------
# Row capping
# ---------------------------------------------------------------------------


def test_returns_all_rows_when_under_the_cap(target_sqlite):
    result = execute_sql(sqlite_conn(target_sqlite), "SELECT * FROM txns", row_limit=10)
    assert result.row_count == 5
    assert result.truncated is False


def test_caps_rows_and_flags_truncation(target_sqlite):
    result = execute_sql(sqlite_conn(target_sqlite), "SELECT * FROM txns", row_limit=3)
    assert result.row_count == 3
    assert result.truncated is True


def test_cap_of_exactly_the_row_count_is_not_truncated(target_sqlite):
    result = execute_sql(sqlite_conn(target_sqlite), "SELECT * FROM txns", row_limit=5)
    assert result.row_count == 5
    assert result.truncated is False


def test_cap_works_on_a_query_that_already_has_its_own_limit(target_sqlite):
    # The cap must not fight a user LIMIT, which is exactly what a SQL rewrite
    # would have risked.
    result = execute_sql(
        sqlite_conn(target_sqlite), "SELECT * FROM txns LIMIT 2", row_limit=10
    )
    assert result.row_count == 2
    assert result.truncated is False


def test_cap_works_on_a_union(target_sqlite):
    result = execute_sql(
        sqlite_conn(target_sqlite),
        "SELECT id FROM txns UNION ALL SELECT id FROM txns",
        row_limit=4,
    )
    assert result.row_count == 4
    assert result.truncated is True


def test_cap_works_on_duplicate_column_names(target_sqlite):
    # A subquery wrapper would have failed on MySQL here. The fetch-side cap
    # does not care.
    result = execute_sql(
        sqlite_conn(target_sqlite), "SELECT id AS a, day AS a FROM txns", row_limit=2
    )
    assert result.row_count == 2
    assert len(result.columns) == 2


def test_columns_come_from_the_result_not_the_schema(target_sqlite):
    result = execute_sql(
        sqlite_conn(target_sqlite),
        "SELECT day, count(*) AS flagged_count FROM txns GROUP BY day",
        row_limit=100,
    )
    assert result.columns == ["day", "flagged_count"]


# ---------------------------------------------------------------------------
# The guard gates execution
# ---------------------------------------------------------------------------


def test_guard_blocks_before_touching_the_database(target_sqlite):
    with pytest.raises(SqlValidationError):
        execute_sql(sqlite_conn(target_sqlite), "DROP TABLE txns", row_limit=10)
    # The table is still there.
    result = execute_sql(sqlite_conn(target_sqlite), "SELECT count(*) FROM txns", 10)
    assert result.rows == [[5]]


def test_multi_statement_blocked(target_sqlite):
    with pytest.raises(SqlValidationError):
        execute_sql(
            sqlite_conn(target_sqlite), "SELECT 1; DROP TABLE txns", row_limit=10
        )


def test_executes_the_sanitised_sql_not_the_original(target_sqlite):
    # If the original were executed, sqlite would see two statements.
    result = execute_sql(
        sqlite_conn(target_sqlite), "SELECT 1 -- ; DROP TABLE txns", row_limit=10
    )
    assert result.rows == [[1]]


# ---------------------------------------------------------------------------
# Row limit resolution
# ---------------------------------------------------------------------------


def test_resolve_row_limit_defaults():
    assert resolve_row_limit(None) == 1000


def test_resolve_row_limit_passes_through_valid():
    assert resolve_row_limit(42) == 42


def test_resolve_row_limit_rejects_over_ceiling():
    with pytest.raises(AppError) as ei:
        resolve_row_limit(999_999)
    assert ei.value.error_code == ErrorCode.ROW_LIMIT_EXCEEDED
    assert ei.value.http_status == 400


def test_resolve_row_limit_rejects_zero():
    with pytest.raises(AppError):
        resolve_row_limit(0)


# ---------------------------------------------------------------------------
# JSON coercion and hashing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (1, 1),
        (True, True),
        (1.5, 1.5),
        ("x", "x"),
        (Decimal("1.10"), "1.10"),
        (date(2026, 8, 22), "2026-08-22"),
        (uuid.UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
        (b"\x00\x01", "AAE="),
    ],
)
def test_to_jsonable(value, expected):
    assert to_jsonable(value) == expected


def test_to_jsonable_datetime_keeps_timezone():
    dt = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    assert to_jsonable(dt) == "2026-08-22T12:00:00+00:00"


def test_every_coerced_value_survives_json_dumps():
    import json

    row = [
        None,
        1,
        1.5,
        "x",
        Decimal("3.14"),
        date(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        uuid.uuid4(),
        b"bytes",
    ]
    json.dumps([to_jsonable(v) for v in row])


def test_hash_is_stable_across_calls():
    a = canonical_hash(["x"], [[1], [2]])
    b = canonical_hash(["x"], [[1], [2]])
    assert a == b
    assert a.startswith("sha256:")


def test_hash_changes_when_a_value_changes():
    assert canonical_hash(["x"], [[1]]) != canonical_hash(["x"], [[2]])


def test_hash_changes_when_columns_change():
    assert canonical_hash(["x"], [[1]]) != canonical_hash(["y"], [[1]])


def test_hash_changes_when_row_order_changes():
    # Order matters for a chart, so it must matter for the hash.
    assert canonical_hash(["x"], [[1], [2]]) != canonical_hash(["x"], [[2], [1]])


def test_hash_of_real_execution_is_stable(target_sqlite):
    conn = sqlite_conn(target_sqlite)
    first = execute_sql(conn, "SELECT * FROM txns ORDER BY id", 100)
    second = execute_sql(conn, "SELECT * FROM txns ORDER BY id", 100)
    assert canonical_hash(first.columns, first.rows) == canonical_hash(
        second.columns, second.rows
    )


def test_hash_changes_when_underlying_data_changes(target_sqlite):
    conn = sqlite_conn(target_sqlite)
    before = execute_sql(conn, "SELECT count(*) FROM txns", 10)

    writable = sqlite3.connect(target_sqlite)
    writable.execute("INSERT INTO txns (day, user_id, amount) VALUES ('2026-08-22',9,1)")
    writable.commit()
    writable.close()

    after = execute_sql(conn, "SELECT count(*) FROM txns", 10)
    assert canonical_hash(before.columns, before.rows) != canonical_hash(
        after.columns, after.rows
    )


# ---------------------------------------------------------------------------
# Chart shaping
# ---------------------------------------------------------------------------


def _query(**over) -> SavedQuery:
    data = dict(
        connection_id="c",
        name="q",
        sql_text="SELECT 1",
        chart_type=ChartType.LINE,
        x_field="day",
        y_field="flagged_count",
    )
    data.update(over)
    return SavedQuery(**data)


def test_chart_echoes_the_mapping():
    chart = build_chart(_query(), ["day", "flagged_count"])
    assert chart["type"] == "line"
    assert chart["x_field"] == "day"
    assert chart["y_field"] == "flagged_count"
    assert chart["series_field"] is None
    assert chart["warnings"] == []


def test_chart_warns_about_a_missing_column_without_failing():
    chart = build_chart(_query(y_field="nope"), ["day"])
    assert chart["y_field"] == "nope"
    assert any("nope" in w for w in chart["warnings"])


def test_chart_warns_when_a_required_field_is_unset():
    chart = build_chart(_query(y_field=None), ["day"])
    assert any("y_field" in w for w in chart["warnings"])


def test_table_chart_needs_no_fields():
    chart = build_chart(
        _query(chart_type=ChartType.TABLE, x_field=None, y_field=None), ["a", "b"]
    )
    assert chart["warnings"] == []


def test_number_chart_needs_only_y_field():
    chart = build_chart(
        _query(chart_type=ChartType.NUMBER, x_field=None, y_field="total"), ["total"]
    )
    assert chart["warnings"] == []


def test_series_field_is_checked_too():
    chart = build_chart(_query(series_field="missing"), ["day", "flagged_count"])
    assert any("series_field" in w for w in chart["warnings"])


def test_to_jsonable_time():
    from datetime import time as dt_time

    assert to_jsonable(dt_time(13, 45, 30)) == "13:45:30"


def test_to_jsonable_nested_list():
    assert to_jsonable([Decimal("1.5"), date(2026, 1, 1)]) == ["1.5", "2026-01-01"]


def test_to_jsonable_tuple_becomes_list():
    assert to_jsonable((1, 2)) == [1, 2]


def test_to_jsonable_dict():
    assert to_jsonable({1: Decimal("2.5")}) == {"1": "2.5"}


def test_to_jsonable_unknown_type_falls_back_to_str():
    class Weird:
        def __str__(self):
            return "weird-value"

    assert to_jsonable(Weird()) == "weird-value"


def test_to_jsonable_memoryview():
    assert to_jsonable(memoryview(b"\x00\x01")) == "AAE="


def test_postgres_array_column_serialises(target_sqlite):
    # A driver returning a list (Postgres arrays, JSON columns) must not break
    # hashing. Exercised directly since SQLite has no array type.
    import json

    assert json.dumps(to_jsonable([[1, 2], [3]])) == "[[1, 2], [3]]"
