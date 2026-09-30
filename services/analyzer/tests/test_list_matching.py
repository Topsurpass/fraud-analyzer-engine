"""Which cells a named list matches. Gate lane: no database, no HTTP.

This corpus is the eval for the lists feature. The service has no model in it,
so, as the README argues for the rest of the engine, the measurable quality bar
is a fixed corpus with a hard zero-mismatch threshold: every row below is a
cell, a list, and the exact answer, and one wrong answer fails the suite.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from app.features.flag_rules.engine import (
    ConditionSpec,
    RuleSpec,
    evaluate,
    evaluate_condition,
)
from app.features.lists import matching
from app.features.lists.matching import list_key, members_from_items
from app.policy.flag_rules import FlagOperator, FlagSeverity

# --- Key normalisation -------------------------------------------------------

#: (item or cell, expected key). Two values with the same key match each other.
KEY_CORPUS = [
    ("abc", "abc"),
    ("ABC", "abc"),
    ("  abc  ", "abc"),
    ("\tAbC\n", "abc"),
    ("STRASSE", "strasse"),
    ("straße", "strasse"),  # casefold, not lower
    ("ÉCOLE", "école"),
    ("2", "2"),
    ("2.0", "2"),
    ("2.00", "2"),
    (" 2 ", "2"),
    (2, "2"),
    (2.0, "2"),
    (Decimal("2.000"), "2"),
    ("100", "1e2"),
    ("1e2", "1e2"),
    ("100.0", "1e2"),
    ("-0", "0"),
    ("0.000", "0"),
    (0, "0"),
    ("-3.50", "-35e-1"),
    ("007", "7"),  # numeric, like `in`; an id column with leading zeros is a text match
    ("", ""),
    ("   ", ""),
    (None, ""),
    (True, "true"),
    (False, "false"),
    ("True", "true"),
    ("nan", "nan"),  # not comparable as a number, so plain text
    ("Infinity", "infinity"),
    ("ACME-1", "acme-1"),
    ("1,000", "1,000"),  # a comma is text, not a thousands separator
]


@pytest.mark.parametrize("value, expected", KEY_CORPUS, ids=[repr(c[0]) for c in KEY_CORPUS])
def test_list_key_corpus(value, expected):
    assert list_key(value) == expected


@pytest.mark.parametrize("text", ["1e999999999", "-1e999999999", "1e-999999999", "1e999999"])
def test_a_huge_exponent_neither_raises_nor_expands(text):
    """Regression: normalize() raised decimal.Overflow on these, which would have
    failed a whole poll on one hostile cell. They key as short text instead."""
    assert len(list_key(text)) < 20
    assert evaluate_condition(_spec(FlagOperator.IN_LIST, [text]), text) is True


@pytest.mark.parametrize(
    "a, b",
    [
        ("1234567890123456789012345678901", "1234567890123456789012345678902"),
        (10**40, 10**40 + 1),
        ("0.1234567890123456789012345678901", "0.1234567890123456789012345678902"),
        ("12345678901234567890123456789", "12345678901234567890123456788"),
        ("1e40", "10000000000000000000000000000000000000001"),
    ],
)
def test_long_numbers_stay_distinct(a, b):
    """Regression: str(normalize()) rounded to 28 digits, so long numeric ids
    that differ only in the last digits shared a key and both flagged."""
    assert list_key(a) != list_key(b)
    condition = ConditionSpec("c", FlagOperator.IN_LIST, members=members_from_items([str(a)]))
    assert evaluate_condition(condition, b) is False
    assert evaluate_condition(condition, a) is True


def test_long_numbers_equal_across_spellings():
    assert list_key("1234567890123456789012345678901") == list_key(1234567890123456789012345678901)
    assert list_key("1234567890123456789012345678901.000") == list_key("1234567890123456789012345678901")


def test_name_key_folds_case_and_unicode_form():
    assert matching.name_key("Éclair") == matching.name_key("éclair")
    assert matching.name_key("e\u0301clair") == matching.name_key("\u00e9clair")  # decomposed
    assert matching.name_key("  Watch ") == matching.name_key("WATCH")
    assert matching.name_key("Straße") == matching.name_key("STRASSE")
    assert matching.name_key("a") != matching.name_key("b")


def test_blank_items_are_not_members():
    assert members_from_items(["a", "", "  ", "b"]) == {"a", "b"}


def test_members_dedupe_by_key():
    assert members_from_items(["A1", "a1 ", "2", "2.0"]) == {"a1", "2"}


# --- Operators over cells ----------------------------------------------------

#: (list items, cell, in_list expected). not_in_list is the complement for every
#: non-null cell and False for NULL, asserted separately below.
CELL_CORPUS = [
    (["a", "b"], "a", True),
    (["a", "b"], "A", True),
    (["a", "b"], "  b ", True),
    (["a", "b"], "c", False),
    (["a", "b"], "ab", False),
    (["a", "b"], "", False),
    (["acme-1"], "ACME-1", True),
    (["1", "2"], 2, True),
    (["1", "2"], 2.0, True),
    (["1", "2"], "2.00", True),
    (["1", "2"], Decimal("2.0"), True),
    (["1.5"], "1.50", True),
    (["1.5"], 1.5, True),
    (["1", "2"], 3, False),
    (["1", "2"], "12", False),
    (["true"], True, True),
    (["true"], False, False),
    (["yes"], True, True),  # a real bool bridges to any spelling, as `in` does
    (["1"], True, True),
    (["t"], True, True),
    (["0"], False, True),
    (["no"], False, True),
    (["yes"], False, False),
    (["true"], "Yes", True),  # two bool words bridge
    (["yes"], "TRUE", True),
    (["1"], "yes", False),  # but a digit never bridges to a word: "NO" is Norway
    (["0"], "NO", False),
    (["no"], 0, False),
    (["true"], 1, False),  # an int cell is a number, not a bool
    (["1"], 1, True),
    (["straße"], "STRASSE", True),
    ([""], "", False),  # a blank item never makes an empty cell a member
    ([], "a", False),
    (["007"], "7", True),
    (["1e2"], "100", True),
    (["   x   "], "x", True),
    (["a,b"], "a", False),  # one item, not two
    (["a,b"], "a,b", True),
]


def _spec(operator, items):
    return ConditionSpec("col", operator, members=members_from_items(items))


@pytest.mark.parametrize("items, cell, expected", CELL_CORPUS)
def test_in_list_corpus(items, cell, expected):
    assert evaluate_condition(_spec(FlagOperator.IN_LIST, items), cell) is expected


@pytest.mark.parametrize("items, cell, expected", CELL_CORPUS)
def test_not_in_list_is_the_complement_over_non_null_cells(items, cell, expected):
    assert evaluate_condition(_spec(FlagOperator.NOT_IN_LIST, items), cell) is (not expected)


@pytest.mark.parametrize("operator", [FlagOperator.IN_LIST, FlagOperator.NOT_IN_LIST])
def test_a_null_cell_matches_neither_list_operator(operator):
    """Three-valued logic, as `not_in` and `neq` do it: unknown is not different."""
    assert evaluate_condition(_spec(operator, ["a"]), None) is False
    assert evaluate_condition(_spec(operator, []), None) is False


@pytest.mark.parametrize("operator", [FlagOperator.IN_LIST, FlagOperator.NOT_IN_LIST])
def test_an_unresolved_list_matches_nothing(operator):
    """members=None must not turn not_in_list into "flag everything"."""
    assert evaluate_condition(ConditionSpec("col", operator), "a") is False


# --- Parity with the inline operators ----------------------------------------

PARITY_ITEMS = ["1001", "1002", "9.5", "NL", "gb"]
PARITY_CELLS = ["1001", 1001, 1002.0, "1003", 9.5, "9.50", "NL", "GB", "us", None, "", "10011"]


def test_in_list_flags_exactly_what_inline_in_flags_on_numbers_and_exact_text():
    """Rubric 1. `in` is case-sensitive, so this parity uses cases where the
    item and cell agree on case; the case-insensitive difference is a documented
    choice and is covered by the corpus above."""
    items = ["1001", "1002", "9.5", "NL"]
    inline = ConditionSpec("col", FlagOperator.IN, value=", ".join(items))
    listed = _spec(FlagOperator.IN_LIST, items)
    for cell in ["1001", 1001, 1002.0, "1003", 9.5, "9.50", "NL", "us", None, "", "10011"]:
        assert evaluate_condition(listed, cell) == evaluate_condition(inline, cell), cell


PARITY_ITEM_SETS = [
    ["1001", "1002", "9.5"],
    ["yes"], ["true"], ["1"], ["t"], ["y"], ["0"], ["no"], ["false"],
    ["NL", "US"],
    ["  spaced  "],
    ["1234567890123456789012345678901"],
    ["true", "9.5", "NL"],
]
PARITY_CELL_POOL = [
    "1001", 1001, 1002.0, "1003", 9.5, "9.50", "9.5", 0, 1, 2, True, False,
    "true", "True", "TRUE", "yes", "Yes", "t", "T", "y", "false", "no", "NO", "n", "f",
    "0", "1", "NL", "US", "nl", "us", "GB", "spaced", "",
    "1234567890123456789012345678901", "1234567890123456789012345678902", None,
]


def _case_consistent(cell, items):
    """`in` is case-sensitive and a list is not. Compare only where the two agree
    on case, which is the domain the parity claim covers."""
    if not isinstance(cell, str):
        return True
    if cell.strip().lower() in {"true", "t", "yes", "y", "false", "f", "no", "n"}:
        return True  # bool words are case-insensitive under `in` too
    return all(
        (cell.strip() == item.strip()) or (cell.strip().lower() != item.strip().lower())
        for item in items
    )


@pytest.mark.parametrize("items", PARITY_ITEM_SETS, ids=lambda i: ",".join(i))
def test_in_list_and_inline_in_agree_over_the_whole_corpus(items):
    """Rubric 1 as a property: for every cell in the pool, list and inline agree
    (case-consistent data), for both operators."""
    disagreements = []
    for operator, listed_operator in ((FlagOperator.IN, FlagOperator.IN_LIST), (FlagOperator.NOT_IN, FlagOperator.NOT_IN_LIST)):
        inline = ConditionSpec("c", operator, value=", ".join(items))
        listed = _spec(listed_operator, items)
        for cell in PARITY_CELL_POOL:
            if not _case_consistent(cell, items):
                continue
            if evaluate_condition(inline, cell) != evaluate_condition(listed, cell):
                disagreements.append((operator.value, cell))
    assert not disagreements


def test_the_one_documented_difference_is_case_on_plain_text():
    inline = ConditionSpec("c", FlagOperator.IN, value="NL")
    listed = _spec(FlagOperator.IN_LIST, ["NL"])
    assert evaluate_condition(inline, "nl") is False
    assert evaluate_condition(listed, "nl") is True


def test_a_cell_with_padding_matches_in_a_list_but_not_inline():
    """Documented: cells are trimmed for a list; `in` compares them as they are."""
    inline = ConditionSpec("c", FlagOperator.IN, value="spaced")
    listed = _spec(FlagOperator.IN_LIST, ["spaced"])
    assert evaluate_condition(inline, "  spaced  ") is False
    assert evaluate_condition(listed, "  spaced  ") is True


def test_the_other_documented_difference_is_unicode_form():
    composed = "\u00e9"
    decomposed = "e\u0301"
    listed = _spec(FlagOperator.IN_LIST, [composed])
    assert evaluate_condition(listed, composed) is True
    assert evaluate_condition(listed, decomposed) is False


def test_not_in_list_matches_not_in_on_the_same_cells():
    items = ["1001", "NL"]
    inline = ConditionSpec("col", FlagOperator.NOT_IN, value=", ".join(items))
    listed = _spec(FlagOperator.NOT_IN_LIST, items)
    for cell in ["1001", 1001, "1003", "NL", "us", None, ""]:
        assert evaluate_condition(listed, cell) == evaluate_condition(inline, cell), cell


def test_whole_result_evaluation_flags_the_listed_rows():
    rule = RuleSpec(
        id="r",
        name="Watch",
        severity=FlagSeverity.HIGH,
        conditions=(_spec(FlagOperator.IN_LIST, ["1001", "nl"]),),
    )
    rows = [["1001"], ["1002"], ["NL"], [None], ["nl "]]
    outcome = evaluate([rule], ["col"], rows)
    assert [flag.index for flag in outcome.rows] == [0, 2, 4]


def test_a_list_condition_combines_with_others_as_and():
    rule = RuleSpec(
        id="r",
        name="Watch and large",
        severity=FlagSeverity.HIGH,
        conditions=(
            ConditionSpec("who", FlagOperator.IN_LIST, members=members_from_items(["a"])),
            ConditionSpec("amount", FlagOperator.GT, value="100"),
        ),
    )
    rows = [["a", 500], ["a", 5], ["b", 500]]
    assert [f.index for f in evaluate([rule], ["who", "amount"], rows).rows] == [0]


# --- Speed budget ------------------------------------------------------------


def test_membership_over_a_50k_item_list_is_inside_the_budget():
    """Building 50,000 keys and testing 50,000 cells end to end. Measured near
    110 ms; the ceiling is 5x that so a slow CI box cannot flake a gate test,
    and still catches an accidental O(n*m) scan (which takes minutes)."""
    items = [f"ACCT-{i:06d}" for i in range(50_000)]
    cells = [f"acct-{i:06d}" for i in range(0, 100_000, 2)]  # half are members

    start = time.perf_counter()
    condition = ConditionSpec("col", FlagOperator.IN_LIST, members=members_from_items(items))
    hits = sum(evaluate_condition(condition, cell) for cell in cells)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert hits == 25_000
    assert elapsed_ms < 600, f"{elapsed_ms:.0f} ms"


def test_the_set_lookup_itself_is_under_100ms_for_50k_cells():
    """The membership test proper, with keys already built. It is a hash lookup;
    a regression to a list scan would blow this by orders of magnitude."""
    members = members_from_items(str(i) for i in range(50_000))
    keys = [list_key(str(i)) for i in range(50_000)]

    start = time.perf_counter()
    hits = sum(key in members for key in keys)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert hits == 50_000
    assert elapsed_ms < 100, f"{elapsed_ms:.0f} ms"


# --- Member cache ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache():
    matching.clear_cache()
    yield
    matching.clear_cache()


def test_bool_tokens_cannot_be_forged_by_a_cell_of_text():
    members = members_from_items(["yes"])
    assert matching.is_member(members, "('bool', True)") is False
    assert matching.is_member(members, "\x00bool") is False


def test_the_cache_loads_items_once_per_version():
    calls = []

    def load():
        calls.append(1)
        return ["a"]

    first = matching.cached_members("L", "v1", load)
    second = matching.cached_members("L", "v1", load)
    assert first is second and len(calls) == 1


def test_a_new_version_misses_the_cache_so_an_edit_is_seen():
    matching.cached_members("L", "v1", lambda: ["a"])
    edited = matching.cached_members("L", "v2", lambda: ["b"])
    assert edited == {"b"}


def test_the_cache_is_bounded():
    for i in range(matching._MAX_CACHED_LISTS + 10):
        matching.cached_members(f"L{i}", "v", lambda: ["a"])
    assert len(matching._cache) == matching._MAX_CACHED_LISTS
