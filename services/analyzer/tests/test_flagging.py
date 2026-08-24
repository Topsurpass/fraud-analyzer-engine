"""The flag-rule evaluator.

Pure functions, no database, no HTTP: this whole file belongs in the gate lane.

The cases that matter are not the obvious ones. Values arriving here have been
through ``query_service.to_jsonable``, so a money column is a *string*, a date
is an ISO *string*, and a driver's boolean might be 1, "t", or True. Every one
of those has to compare the way an analyst expects.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import FlagOperator, FlagSeverity
from app.services.flagging import (
    ConditionSpec,
    RuleSpec,
    as_number,
    evaluate,
    evaluate_condition,
    specs_from_models,
    split_list,
)


def cond(column: str, operator: FlagOperator, value=None, value2=None) -> ConditionSpec:
    return ConditionSpec(
        column_name=column, operator=operator, value=value, value2=value2
    )


def rule(*conditions: ConditionSpec, id="r1", name="rule", severity=FlagSeverity.HIGH,
         enabled=True) -> RuleSpec:
    return RuleSpec(
        id=id, name=name, severity=severity, conditions=conditions, enabled=enabled
    )


# ---------------------------------------------------------------------------
# Number coercion, which every ordering comparison rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (5, Decimal(5)),
        (5.5, Decimal("5.5")),
        ("5", Decimal(5)),
        ("  5.25  ", Decimal("5.25")),
        ("-3", Decimal(-3)),
        ("1e3", Decimal(1000)),
        (Decimal("900.00"), Decimal("900.00")),
    ],
)
def test_as_number_parses_what_is_numeric(value, expected):
    assert as_number(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "abc", "12abc", [], {}, "NaN", "Infinity", float("nan"),
     float("inf"), float("-inf"), Decimal("NaN")],
)
def test_as_number_refuses_what_is_not(value):
    assert as_number(value) is None


def test_booleans_are_never_numbers():
    """``True > 0`` is legal Python and meaningless as a threshold."""
    assert as_number(True) is None
    assert as_number(False) is None


def test_money_keeps_decimal_precision():
    """The reason as_number returns Decimal and not float.

    to_jsonable converts Decimal to str precisely so a hash cannot drift across
    platforms. Parsing it back as float would reintroduce exactly the error
    that conversion avoided, and a threshold sitting on the boundary would flag
    inconsistently.
    """
    cell = "0.30000000000000004"  # what 0.1 + 0.2 becomes in binary float
    assert evaluate_condition(cond("x", FlagOperator.GT, "0.3"), cell) is True
    assert evaluate_condition(cond("x", FlagOperator.GT, "0.3"), "0.3") is False


# ---------------------------------------------------------------------------
# The operator matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator,value,cell,expected",
    [
        # Ordering, numeric.
        (FlagOperator.GT, "500", 900, True),
        (FlagOperator.GT, "500", 500, False),
        (FlagOperator.GTE, "500", 500, True),
        (FlagOperator.LT, "500", 100, True),
        (FlagOperator.LTE, "500", 500, True),
        # Ordering against a Decimal that arrived as a string.
        (FlagOperator.GT, "500", "900.25", True),
        (FlagOperator.GT, "900.25", "900.24", False),
        (FlagOperator.GTE, "900.25", "900.25", True),
        # Ordering, lexical. ISO 8601 sorts correctly as text, which is the
        # entire reason a date column needs no date parsing here.
        (FlagOperator.GTE, "2026-08-19", "2026-08-20", True),
        (FlagOperator.GTE, "2026-08-19", "2026-08-18", False),
        (FlagOperator.LT, "2026-08-19", "2026-08-18", True),
        # Equality, numeric across representations.
        (FlagOperator.EQ, "500", "500.00", True),
        (FlagOperator.EQ, "500", 500, True),
        (FlagOperator.EQ, "500", "500.01", False),
        (FlagOperator.NEQ, "500", "500.00", False),
        (FlagOperator.NEQ, "500", 501, True),
        # Equality, text. Case-sensitive, matching Postgres's `=`.
        (FlagOperator.EQ, "NG", "NG", True),
        (FlagOperator.EQ, "NG", "ng", False),
        (FlagOperator.NEQ, "NG", "ng", True),
        # Substring operators, case-insensitive by design.
        (FlagOperator.CONTAINS, "geo", "geo mismatch", True),
        (FlagOperator.CONTAINS, "GEO", "geo mismatch", True),
        (FlagOperator.CONTAINS, "velocity", "geo mismatch", False),
        (FlagOperator.NOT_CONTAINS, "velocity", "geo mismatch", True),
        (FlagOperator.STARTS_WITH, "geo", "geo mismatch", True),
        (FlagOperator.STARTS_WITH, "mismatch", "geo mismatch", False),
        # Membership.
        (FlagOperator.IN, "NG,GH,KE", "GH", True),
        (FlagOperator.IN, "NG,GH,KE", "US", False),
        (FlagOperator.IN, " NG , GH ", "GH", True),
        (FlagOperator.NOT_IN, "NG,GH", "US", True),
        (FlagOperator.NOT_IN, "NG,GH", "NG", False),
        (FlagOperator.IN, "1,2,3", "2.0", True),
        # Range.
        (FlagOperator.BETWEEN, "100", 500, True),
        (FlagOperator.BETWEEN, "100", 50, False),
        (FlagOperator.BETWEEN, "100", 1000, False),
        (FlagOperator.BETWEEN, "100", 100, True),
        (FlagOperator.BETWEEN, "100", 900, True),
    ],
)
def test_operator_matrix(operator, value, cell, expected):
    value2 = "900" if operator is FlagOperator.BETWEEN else None
    assert evaluate_condition(cond("x", operator, value, value2), cell) is expected


def test_between_normalises_reversed_bounds():
    """'between 900 and 100' is unambiguous about intent."""
    reversed_bounds = cond("x", FlagOperator.BETWEEN, "900", "100")
    assert evaluate_condition(reversed_bounds, 500) is True
    assert evaluate_condition(reversed_bounds, 50) is False


def test_between_on_iso_dates_is_lexical():
    condition = cond("x", FlagOperator.BETWEEN, "2026-08-19", "2026-08-21")
    assert evaluate_condition(condition, "2026-08-20") is True
    assert evaluate_condition(condition, "2026-08-18") is False


def test_between_rejects_a_non_numeric_cell_against_numeric_bounds():
    condition = cond("x", FlagOperator.BETWEEN, "100", "900")
    assert evaluate_condition(condition, "not a number") is False


# ---------------------------------------------------------------------------
# NULL follows SQL, not Python
# ---------------------------------------------------------------------------


def test_is_null_and_is_not_null_are_the_only_operators_that_see_null():
    assert evaluate_condition(cond("x", FlagOperator.IS_NULL), None) is True
    assert evaluate_condition(cond("x", FlagOperator.IS_NOT_NULL), None) is False
    assert evaluate_condition(cond("x", FlagOperator.IS_NULL), "value") is False
    assert evaluate_condition(cond("x", FlagOperator.IS_NOT_NULL), "value") is True


@pytest.mark.parametrize(
    "operator,value",
    [
        (FlagOperator.EQ, "x"),
        (FlagOperator.NEQ, "x"),
        (FlagOperator.GT, "0"),
        (FlagOperator.LT, "999"),
        (FlagOperator.CONTAINS, "x"),
        (FlagOperator.NOT_CONTAINS, "x"),
        (FlagOperator.STARTS_WITH, "x"),
        (FlagOperator.IN, "x,y"),
        (FlagOperator.NOT_IN, "x,y"),
        (FlagOperator.BETWEEN, "0"),
    ],
)
def test_null_never_matches_a_comparison(operator, value):
    """Three-valued logic: "unknown" is not "different".

    Doing this the Python way would flag every row with a missing value the
    instant anyone wrote a `!=` rule, which is the opposite of useful.
    """
    value2 = "999" if operator is FlagOperator.BETWEEN else None
    assert evaluate_condition(cond("x", operator, value, value2), None) is False


# ---------------------------------------------------------------------------
# Driver booleans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell", [True, 1, "1", "true", "t", "TRUE"])
def test_truthy_flags_across_drivers_compare_as_text_or_number(cell):
    """SQLite gives 1, Postgres gives True, some drivers give 't'.

    A rule written as `eq 1` should catch the numeric spellings and a rule
    written as `eq true` should catch the textual ones. What must never happen
    is a boolean silently participating in an ordering comparison.
    """
    numeric = evaluate_condition(cond("f", FlagOperator.EQ, "1"), cell)
    textual = evaluate_condition(cond("f", FlagOperator.EQ, "true"), cell)
    assert numeric or textual


def test_boolean_true_is_not_greater_than_zero():
    """Without the bool guard the text fallback answers this: "true" > "0"."""
    assert evaluate_condition(cond("f", FlagOperator.GT, "0"), True) is False
    assert evaluate_condition(cond("f", FlagOperator.LT, "0"), False) is False
    assert evaluate_condition(cond("f", FlagOperator.GTE, "1"), True) is False


@pytest.mark.parametrize("spelling", ["true", "1", "yes", "t", "TRUE", "Y"])
def test_a_real_boolean_cell_accepts_any_spelling(spelling):
    assert evaluate_condition(cond("f", FlagOperator.EQ, spelling), True) is True
    assert evaluate_condition(cond("f", FlagOperator.EQ, spelling), False) is False


@pytest.mark.parametrize("spelling", ["false", "0", "no", "f", "N"])
def test_a_real_boolean_cell_accepts_any_false_spelling(spelling):
    assert evaluate_condition(cond("f", FlagOperator.EQ, spelling), False) is True
    assert evaluate_condition(cond("f", FlagOperator.EQ, spelling), True) is False


def test_boolean_words_compare_to_each_other():
    """A text column holding 't' should still answer a rule written 'true'."""
    assert evaluate_condition(cond("f", FlagOperator.EQ, "true"), "t") is True
    assert evaluate_condition(cond("f", FlagOperator.EQ, "TRUE"), "yes") is True
    assert evaluate_condition(cond("f", FlagOperator.EQ, "false"), "n") is True
    assert evaluate_condition(cond("f", FlagOperator.EQ, "true"), "f") is False


def test_the_boolean_bridge_does_not_reach_norway():
    """`country eq 0` must not match "NO".

    This is why the word set excludes the digits. Widening it so that digits
    and words compare as booleans would silently turn an ISO country code into
    a false, and a rule looking for a zero amount would flag every Norwegian
    row in the table.
    """
    assert evaluate_condition(cond("country", FlagOperator.EQ, "0"), "NO") is False
    assert evaluate_condition(cond("country", FlagOperator.EQ, "1"), "Y") is False
    assert evaluate_condition(cond("country", FlagOperator.EQ, "NO"), "NO") is True


def test_a_boolean_cell_against_a_non_boolean_value_never_matches():
    assert evaluate_condition(cond("f", FlagOperator.EQ, "banana"), True) is False
    assert evaluate_condition(cond("f", FlagOperator.NEQ, "banana"), True) is True


# ---------------------------------------------------------------------------
# split_list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a,b,c", ["a", "b", "c"]),
        (" a , b ", ["a", "b"]),
        ("a,,b", ["a", "b"]),
        ("", []),
        (None, []),
        (",,,", []),
    ],
)
def test_split_list(raw, expected):
    assert split_list(raw) == expected


# ---------------------------------------------------------------------------
# Whole-result evaluation
# ---------------------------------------------------------------------------


COLUMNS = ["day", "amount", "comment"]
ROWS = [
    ["2026-08-18", "10.5", "ok"],
    ["2026-08-19", "900.0", "velocity"],
    ["2026-08-19", "12.0", None],
    ["2026-08-20", "750.25", "geo mismatch"],
    ["2026-08-21", "20.0", "ok"],
]


def test_a_rule_ands_its_conditions():
    outcome = evaluate(
        [rule(cond("amount", FlagOperator.GT, "500"),
              cond("day", FlagOperator.GTE, "2026-08-20"))],
        COLUMNS,
        ROWS,
    )
    # Row 1 is over 500 but too early; row 3 satisfies both.
    assert outcome.flagged_indices() == {3}


def test_rows_are_flagged_when_any_rule_matches():
    outcome = evaluate(
        [
            rule(cond("amount", FlagOperator.GT, "500"), id="big", name="Big"),
            rule(cond("comment", FlagOperator.IS_NULL), id="nc", name="No comment"),
        ],
        COLUMNS,
        ROWS,
    )
    assert outcome.flagged_indices() == {1, 2, 3}


def test_a_row_records_every_rule_that_caught_it():
    outcome = evaluate(
        [
            rule(cond("amount", FlagOperator.GT, "500"), id="big", name="Big"),
            rule(cond("day", FlagOperator.EQ, "2026-08-19"), id="day", name="Day"),
        ],
        COLUMNS,
        ROWS,
    )
    by_index = {row.index: row.rule_ids for row in outcome.rows}
    assert by_index[1] == ["big", "day"]
    assert by_index[2] == ["day"]


def test_rule_tally_counts_matches_including_zero():
    outcome = evaluate(
        [
            rule(cond("amount", FlagOperator.GT, "500"), id="big", name="Big"),
            rule(cond("amount", FlagOperator.GT, "999999"), id="huge", name="Huge"),
        ],
        COLUMNS,
        ROWS,
    )
    tally = {hit.id: hit.matched for hit in outcome.rules}
    assert tally == {"big": 2, "huge": 0}


def test_a_rule_that_matched_nothing_still_appears():
    """A rule vanishing from the summary looks like it was never saved."""
    outcome = evaluate(
        [rule(cond("amount", FlagOperator.GT, "999999"), id="huge", name="Huge")],
        COLUMNS,
        ROWS,
    )
    assert [hit.id for hit in outcome.rules] == ["huge"]
    assert outcome.flagged_count == 0


def test_disabled_rules_are_skipped_entirely():
    outcome = evaluate(
        [rule(cond("amount", FlagOperator.GT, "500"), enabled=False)],
        COLUMNS,
        ROWS,
    )
    assert outcome.flagged_count == 0
    assert outcome.rules == []


def test_no_rules_flags_nothing():
    outcome = evaluate([], COLUMNS, ROWS)
    assert outcome.flagged_count == 0
    assert outcome.warnings == []


def test_empty_result_set_is_not_an_error():
    outcome = evaluate([rule(cond("amount", FlagOperator.GT, "1"))], COLUMNS, [])
    assert outcome.flagged_count == 0


# ---------------------------------------------------------------------------
# Degenerate rules warn instead of failing
# ---------------------------------------------------------------------------


def test_an_unknown_column_warns_and_matches_nothing():
    """The usual cause is editing the SELECT list after writing the rule."""
    outcome = evaluate(
        [rule(cond("nonexistent", FlagOperator.GT, "1"), name="Ghost")],
        COLUMNS,
        ROWS,
    )
    assert outcome.flagged_count == 0
    assert len(outcome.warnings) == 1
    assert "nonexistent" in outcome.warnings[0]
    assert "Ghost" in outcome.warnings[0]


def test_one_bad_column_disables_only_its_own_rule():
    outcome = evaluate(
        [
            rule(cond("nope", FlagOperator.GT, "1"), id="bad", name="Bad"),
            rule(cond("amount", FlagOperator.GT, "500"), id="good", name="Good"),
        ],
        COLUMNS,
        ROWS,
    )
    assert outcome.flagged_indices() == {1, 3}
    assert len(outcome.warnings) == 1


def test_a_rule_with_no_conditions_matches_nothing_and_warns():
    """Unreachable via the API, which rejects it at validation.

    Matching nothing is the safe reading of a rule that asks for nothing; the
    alternative reading, vacuous truth, would flag every row in the result.
    """
    outcome = evaluate([rule(name="Empty")], COLUMNS, ROWS)
    assert outcome.flagged_count == 0
    assert "no conditions" in outcome.warnings[0]


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


def test_as_dict_is_primitive_and_ordered():
    outcome = evaluate(
        [rule(cond("amount", FlagOperator.GT, "500"), id="big", name="Big")],
        COLUMNS,
        ROWS,
    )
    payload = outcome.as_dict()
    assert payload == {
        "flagged_count": 2,
        "rows": [
            {"index": 1, "rule_ids": ["big"]},
            {"index": 3, "rule_ids": ["big"]},
        ],
        "rules": [{"id": "big", "name": "Big", "severity": "high", "matched": 2}],
        "warnings": [],
    }


def test_only_flagged_rows_are_carried():
    """A parallel array over 10,000 rows would be 9,990 empty entries."""
    rows = [[str(n)] for n in range(1000)]
    outcome = evaluate(
        [rule(cond("n", FlagOperator.GTE, "999"))], ["n"], rows
    )
    assert len(outcome.as_dict()["rows"]) == 1


# ---------------------------------------------------------------------------
# ORM adaptation
# ---------------------------------------------------------------------------


class _FakeCondition:
    def __init__(self, position, column_name, operator, value=None, value2=None):
        self.position = position
        self.column_name = column_name
        self.operator = operator
        self.value = value
        self.value2 = value2


class _FakeRule:
    def __init__(self, id, name, position, conditions, severity=FlagSeverity.LOW,
                 enabled=True):
        self.id = id
        self.name = name
        self.position = position
        self.conditions = conditions
        self.severity = severity
        self.enabled = enabled


def test_specs_from_models_sorts_rules_and_conditions_by_position():
    models = [
        _FakeRule("b", "Second", 1, [
            _FakeCondition(1, "y", FlagOperator.EQ, "2"),
            _FakeCondition(0, "x", FlagOperator.EQ, "1"),
        ]),
        _FakeRule("a", "First", 0, [_FakeCondition(0, "z", FlagOperator.EQ, "3")]),
    ]
    specs = specs_from_models(models)
    assert [spec.id for spec in specs] == ["a", "b"]
    assert [c.column_name for c in specs[1].conditions] == ["x", "y"]
