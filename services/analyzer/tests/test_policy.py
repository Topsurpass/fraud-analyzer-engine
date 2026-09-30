"""The editable knobs in ``app/policy/`` do what their headers promise.

These are the tests to read before changing a policy file. They pin the two ways
a hand edit goes wrong: a typo that quietly matches nothing, and a new option
that nothing downstream knows how to handle.
"""

from __future__ import annotations

import pytest

from app.errors import ErrorCode, SqlValidationError
from app.features.charts.schemas import QueryChartBase
from app.features.flag_rules.engine import ConditionSpec, evaluate_condition
from app.policy import sql_allowlist
from app.features.flag_rules.flagged_rows import _worst
from app.policy.chart_types import REQUIRED_FIELDS, ChartType
from app.policy.flag_rules import (
    BINARY_OPERATORS,
    NULLARY_OPERATORS,
    SEVERITY_ORDER,
    FlagOperator,
    FlagSeverity,
)
from app.security import sql_guard

# --- SQL allowlist -----------------------------------------------------------


def test_shipped_sql_policy_is_well_formed():
    sql_allowlist.validate_policy()


@pytest.mark.parametrize(
    "attr, bad",
    [
        ("FORBIDDEN_KEYWORDS", "merge"),  # keywords are upper case
        ("FORBIDDEN_NAMES", "OUTFILE"),  # names are lower case
        ("POSTGRES_FUNCTIONS", "Pg_Sleep"),  # functions are lower case
        ("FORBIDDEN_LOCKING_WORDS", "share"),
        ("ALLOWED_STATEMENT_TYPES", "select"),
    ],
)
def test_a_wrong_case_entry_stops_the_service_instead_of_failing_open(monkeypatch, attr, bad):
    """A lower-case keyword matches nothing, so the edit would silently do no work."""
    monkeypatch.setattr(sql_allowlist, attr, getattr(sql_allowlist, attr) | {bad})
    monkeypatch.setattr(
        sql_allowlist,
        "FORBIDDEN_FUNCTIONS",
        sql_allowlist.POSTGRES_FUNCTIONS
        | sql_allowlist.MYSQL_FUNCTIONS
        | sql_allowlist.SQLITE_FUNCTIONS,
    )
    with pytest.raises(ValueError, match="sql_allowlist"):
        sql_allowlist.validate_policy()


def test_adding_a_keyword_to_the_policy_takes_effect(monkeypatch):
    """The documented way to block a clause: add it to the list, nothing else."""
    assert sql_guard.validate_select("SELECT DISTINCT a FROM t")

    monkeypatch.setattr(
        sql_guard, "FORBIDDEN_KEYWORDS", sql_allowlist.FORBIDDEN_KEYWORDS | {"DISTINCT"}
    )
    sql_guard.clear_validation_cache()
    try:
        with pytest.raises(SqlValidationError) as ei:
            sql_guard.validate_select("SELECT DISTINCT a FROM t")
        assert ei.value.error_code == ErrorCode.FORBIDDEN_KEYWORD
    finally:
        monkeypatch.undo()
        sql_guard.clear_validation_cache()


def test_removing_a_function_from_the_policy_allows_it(monkeypatch):
    """The documented way to allow a blocked function: remove it from its group."""
    with pytest.raises(SqlValidationError):
        sql_guard.validate_select("SELECT sleep(1)")

    monkeypatch.setattr(sql_guard, "FORBIDDEN_FUNCTIONS", sql_allowlist.FORBIDDEN_FUNCTIONS - {"sleep"})
    sql_guard.clear_validation_cache()
    try:
        assert sql_guard.validate_select("SELECT sleep(1)")
    finally:
        monkeypatch.undo()
        sql_guard.clear_validation_cache()


def test_the_guard_reads_its_lists_from_the_policy_file():
    for name in (
        "FORBIDDEN_KEYWORDS",
        "FORBIDDEN_NAMES",
        "FORBIDDEN_FUNCTIONS",
        "FORBIDDEN_LOCKING_WORDS",
        "ALLOWED_STATEMENT_TYPES",
    ):
        assert getattr(sql_guard, name) is getattr(sql_allowlist, name), name


# --- Flag rule operators and severities --------------------------------------

#: One row per operator: (operator, cell, value, value2). Each must evaluate True.
#: Adding an operator to ``app/policy/flag_rules.py`` without adding its row here
#: fails ``test_every_operator_has_an_evaluator``, which is the reminder to teach
#: ``evaluate_condition`` about it.
_OPERATOR_TRUE_CASES = [
    (FlagOperator.GT, 10, "5", None),
    (FlagOperator.GTE, 5, "5", None),
    (FlagOperator.LT, 1, "5", None),
    (FlagOperator.LTE, 5, "5", None),
    (FlagOperator.EQ, "abc", "abc", None),
    (FlagOperator.NEQ, "abc", "xyz", None),
    (FlagOperator.CONTAINS, "hello world", "WORLD", None),
    (FlagOperator.NOT_CONTAINS, "hello", "zzz", None),
    (FlagOperator.STARTS_WITH, "hello", "HE", None),
    (FlagOperator.IN, "b", "a, b, c", None),
    (FlagOperator.NOT_IN, "z", "a, b, c", None),
    (FlagOperator.IS_NULL, None, None, None),
    (FlagOperator.IS_NOT_NULL, 1, None, None),
    (FlagOperator.BETWEEN, 5, "1", "10"),
]


def test_every_operator_has_an_evaluator():
    covered = {case[0] for case in _OPERATOR_TRUE_CASES}
    assert covered == set(FlagOperator), (
        f"add a case here and an evaluator in engine.evaluate_condition for: "
        f"{sorted(o.value for o in set(FlagOperator) - covered)}"
    )


@pytest.mark.parametrize("operator, cell, value, value2", _OPERATOR_TRUE_CASES)
def test_operator_matches_its_documented_case(operator, cell, value, value2):
    condition = ConditionSpec("col", operator, value, value2)
    assert evaluate_condition(condition, cell) is True


def test_operator_arity_sets_only_name_real_operators():
    assert NULLARY_OPERATORS <= set(FlagOperator)
    assert BINARY_OPERATORS <= set(FlagOperator)
    assert not (NULLARY_OPERATORS & BINARY_OPERATORS)


def test_every_severity_has_a_unique_rank():
    assert set(SEVERITY_ORDER) == set(FlagSeverity)
    assert len(set(SEVERITY_ORDER.values())) == len(SEVERITY_ORDER)


# --- Chart types --------------------------------------------------------------


@pytest.mark.parametrize("chart_type", list(ChartType))
def test_every_chart_type_is_accepted_by_the_api_schema(chart_type):
    chart = QueryChartBase(name="c", chart_type=chart_type.value)
    assert chart.chart_type is chart_type


def test_chart_type_values_are_stable_snake_case():
    for chart_type in ChartType:
        assert chart_type.value == chart_type.value.lower()
        assert " " not in chart_type.value and "-" not in chart_type.value
        assert len(chart_type.value) <= 20, "enum_column stores chart types in VARCHAR(20)"


@pytest.mark.parametrize("chart_type", list(ChartType))
def test_every_chart_type_declares_its_required_fields(chart_type):
    """A missing entry silently drops the 'needs y_field' warning for that type."""
    assert chart_type in REQUIRED_FIELDS
    assert set(REQUIRED_FIELDS[chart_type]) <= {"x_field", "y_field", "series_field"}


@pytest.mark.parametrize("severity", list(FlagSeverity))
def test_every_severity_is_rankable_where_the_queue_ranks(severity):
    """Regression: flagged_rows kept its own three-entry rank table, so a fourth
    severity added to the policy raised KeyError when a rule with it matched."""
    assert _worst([severity.value]) is severity
    worst = max(FlagSeverity, key=lambda s: SEVERITY_ORDER[s])
    assert _worst([s.value for s in FlagSeverity]) is worst
