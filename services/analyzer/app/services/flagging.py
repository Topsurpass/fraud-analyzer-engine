"""Decide which returned rows a user's flag rules mark as interesting.

This module is deliberately pure: it takes rule specifications and an already
executed result set, and returns which rows matched. It performs no I/O, holds
no state, and never touches SQL.

That last point is the whole design. Conditions are evaluated here, in Python,
over rows the database already handed back, so no part of a user-authored
condition is ever spliced into a statement. ``app.security.sql_guard`` keeps
seeing byte-identical SQL to what the analyst wrote, and the same evaluation
runs against Postgres, MySQL and SQLite without a dialect layer. The cost of
that choice is bounded visibility: flagging only ever sees rows inside the
query's ``row_limit``.

**Boolean shape.** A rule matches a row when *all* of its conditions match. A
row is flagged when *any* enabled rule matches. That covers AND and OR without
an expression parser, which is the only reason there is no expression parser.

**Why comparison is fiddly.** By the time rows reach here they have been through
``query_service.to_jsonable``, which converts ``Decimal`` to ``str`` (so a hash
cannot drift across platforms) and dates to ISO strings. So ``amount > 500``
usually compares the string ``"500.25"`` against the number 500, and
``day >= '2026-08-19'`` compares two ISO strings. Both have to work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

from app.models.enums import FlagOperator, FlagSeverity

#: Low to high. Callers sort and group by this rather than by the enum's
#: declaration order, which is not a promise the language makes.
SEVERITY_ORDER: dict[FlagSeverity, int] = {
    FlagSeverity.LOW: 0,
    FlagSeverity.MEDIUM: 1,
    FlagSeverity.HIGH: 2,
}


@dataclass(slots=True, frozen=True)
class ConditionSpec:
    """One comparison against one result column."""

    column_name: str
    operator: FlagOperator
    value: str | None = None
    value2: str | None = None


@dataclass(slots=True, frozen=True)
class RuleSpec:
    """A named set of conditions, ANDed together."""

    id: str
    name: str
    severity: FlagSeverity
    conditions: tuple[ConditionSpec, ...]
    enabled: bool = True


@dataclass(slots=True)
class RowFlag:
    """One flagged row: where it is, and which rules caught it."""

    index: int
    rule_ids: list[str]


@dataclass(slots=True)
class RuleHit:
    """Per-rule tally, so the UI can show a rule's reach without rescanning."""

    id: str
    name: str
    severity: FlagSeverity
    matched: int


@dataclass(slots=True)
class FlagOutcome:
    """Which rows matched, which rules did the matching, and what went wrong.

    ``rows`` holds *only* flagged rows. A parallel array over every row would
    be mostly empty entries, and a 10,000-row result would carry 10,000 of them
    to say nothing.
    """

    rows: list[RowFlag] = field(default_factory=list)
    rules: list[RuleHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return len(self.rows)

    def flagged_indices(self) -> set[int]:
        return {row.index for row in self.rows}

    def as_dict(self) -> dict:
        """The wire shape. Ordered and primitive, so it hashes stably."""
        return {
            "flagged_count": self.flagged_count,
            "rows": [
                {"index": row.index, "rule_ids": row.rule_ids} for row in self.rows
            ],
            "rules": [
                {
                    "id": hit.id,
                    "name": hit.name,
                    "severity": hit.severity.value,
                    "matched": hit.matched,
                }
                for hit in self.rules
            ],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def as_number(value: Any) -> Decimal | None:
    """Return ``value`` as a Decimal, or None if it is not a number.

    ``Decimal`` rather than ``float`` because the values that arrive here are
    frequently *strings that used to be Decimals* -- ``to_jsonable`` converts
    them precisely so that money does not round-trip through binary floating
    point. Parsing them back as floats would reintroduce the error the string
    conversion existed to avoid: ``0.1 + 0.2 > 0.3`` is True in float and False
    in Decimal, and a threshold rule sitting exactly on a boundary would flag
    inconsistently.

    ``bool`` is excluded on purpose even though it is an ``int`` subclass.
    ``True > 0`` is technically valid Python and completely meaningless as a
    flag condition; treating booleans as text keeps ``eq true`` working the way
    an analyst expects.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return None if value.is_nan() else value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # NaN and infinities cannot participate in an ordering that means
        # anything. to_jsonable already nulls them out of result rows, but a
        # rule's own value string could still spell one.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        # "nan" and "infinity" parse as Decimal but are not comparable.
        return None if not parsed.is_finite() else parsed
    return None


#: Words a two-valued column spells itself with, excluding the digits 0 and 1.
#: Kept word-only on purpose. Bridging digits to words here would make the rule
#: ``country eq 0`` match a cell of ``"NO"``, which is Norway's ISO code, not a
#: false. Digits reach booleans only through a genuine ``bool`` cell below.
_TRUE_WORDS = frozenset({"true", "t", "yes", "y"})
_FALSE_WORDS = frozenset({"false", "f", "no", "n"})
_BOOL_WORDS = _TRUE_WORDS | _FALSE_WORDS


def as_bool(value: Any) -> bool | None:
    """Interpret a driver's idea of a boolean, or None if it is not one.

    Drivers disagree: Postgres hands back ``True``, SQLite hands back ``1``,
    and a text column holding a flag might say ``"t"`` or ``"yes"``. An analyst
    should not have to know which of those their database chose.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS or text == "1":
            return True
        if text in _FALSE_WORDS or text == "0":
            return False
    return None


def _is_bool_word(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _BOOL_WORDS


def as_text(value: Any) -> str:
    """Render a cell as text for string comparisons."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def split_list(raw: str | None) -> list[str]:
    """Split an ``in``/``not_in`` value on commas, dropping empty entries.

    No quoting or escaping: a comma inside a value cannot be expressed. That is
    a real limit, and the honest alternative -- inventing a mini CSV dialect in
    a text column -- costs more than it returns for a list of country codes or
    status strings.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Single-condition evaluation
# ---------------------------------------------------------------------------


def _compare_ordered(cell: Any, expected: str | None, operator: FlagOperator) -> bool:
    """gt/gte/lt/lte, numerically when possible and lexically otherwise.

    The lexical fallback is not a consolation prize: ISO 8601 sorts correctly
    as text, so ``day >= '2026-08-19'`` works on the string a date column
    becomes without anyone having to parse a date format.
    """
    if isinstance(cell, bool):
        # A boolean has no position on a scale. Without this the text fallback
        # below would answer it anyway -- "true" > "0" is perfectly true
        # lexically -- and every True row would satisfy `> 0`.
        return False

    left_number = as_number(cell)
    right_number = as_number(expected)

    if left_number is not None and right_number is not None:
        left: Any = left_number
        right: Any = right_number
    else:
        left = as_text(cell)
        right = as_text(expected)

    if operator is FlagOperator.GT:
        return left > right
    if operator is FlagOperator.GTE:
        return left >= right
    if operator is FlagOperator.LT:
        return left < right
    return left <= right


def _equals(cell: Any, expected: str | None) -> bool:
    """Equality: numeric when both sides are numbers, exact text otherwise.

    Numeric first is what makes ``eq 500`` match a ``Decimal`` column that
    arrived as ``"500.00"``. Text comparison is case-sensitive, matching what
    ``=`` does in Postgres; ``contains`` and ``starts_with`` are the
    case-insensitive operators, and that split is documented for users rather
    than guessed at.

    Booleans get their own path so that ``eq true``, ``eq 1`` and ``eq yes``
    all catch a true cell whatever the driver spelled it as. The bridge is
    narrow by design: a genuine ``bool`` cell accepts any spelling, but two
    strings only compare as booleans when *both* are words. That is what stops
    ``country eq 0`` from matching Norway's ``"NO"``.
    """
    if isinstance(cell, bool):
        expected_bool = as_bool(expected)
        return expected_bool is not None and cell is expected_bool

    if _is_bool_word(cell) and _is_bool_word(expected):
        return as_bool(cell) is as_bool(expected)

    left_number = as_number(cell)
    right_number = as_number(expected)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return as_text(cell) == as_text(expected)


def evaluate_condition(condition: ConditionSpec, cell: Any) -> bool:
    """Whether one cell satisfies one condition.

    NULL follows SQL's three-valued logic rather than Python's: a NULL cell
    matches ``is_null`` and nothing else. In particular it does *not* satisfy
    ``neq`` or ``not_contains``, because "unknown" is not "different". Doing it
    the Python way would flag every row with a missing value the moment anyone
    wrote a ``!=`` rule, which is the opposite of useful.
    """
    operator = condition.operator

    if operator is FlagOperator.IS_NULL:
        return cell is None
    if operator is FlagOperator.IS_NOT_NULL:
        return cell is not None

    if cell is None:
        return False

    if operator in (FlagOperator.GT, FlagOperator.GTE, FlagOperator.LT, FlagOperator.LTE):
        return _compare_ordered(cell, condition.value, operator)

    if operator is FlagOperator.EQ:
        return _equals(cell, condition.value)
    if operator is FlagOperator.NEQ:
        return not _equals(cell, condition.value)

    if operator is FlagOperator.CONTAINS:
        return as_text(condition.value).lower() in as_text(cell).lower()
    if operator is FlagOperator.NOT_CONTAINS:
        return as_text(condition.value).lower() not in as_text(cell).lower()
    if operator is FlagOperator.STARTS_WITH:
        return as_text(cell).lower().startswith(as_text(condition.value).lower())

    if operator in (FlagOperator.IN, FlagOperator.NOT_IN):
        members = split_list(condition.value)
        hit = any(_equals(cell, member) for member in members)
        return hit if operator is FlagOperator.IN else not hit

    if operator is FlagOperator.BETWEEN:
        low_raw, high_raw = condition.value, condition.value2
        low_number, high_number = as_number(low_raw), as_number(high_raw)
        if low_number is not None and high_number is not None:
            low: Any = min(low_number, high_number)
            high: Any = max(low_number, high_number)
            cell_value: Any = as_number(cell)
            if cell_value is None:
                return False
        else:
            # Reversed bounds are normalised rather than rejected. A rule
            # reading "between 900 and 100" is unambiguous about intent and
            # silently matching nothing would be the least helpful reading.
            low, high = sorted((as_text(low_raw), as_text(high_raw)))
            cell_value = as_text(cell)
        return low <= cell_value <= high

    # Unreachable while FlagOperator is exhaustive above. Not matching is the
    # safe direction for an operator this build does not understand.
    return False  # pragma: no cover


# ---------------------------------------------------------------------------
# Whole-result evaluation
# ---------------------------------------------------------------------------


def evaluate(
    rules: Iterable[RuleSpec],
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> FlagOutcome:
    """Apply every enabled rule to every row.

    A condition naming a column the result does not contain never matches, and
    is reported as a warning instead of an error. That mirrors ``build_chart``:
    the rows are still worth showing, and failing the whole run would hide data
    the analyst can plainly see is there. The usual cause is editing the SELECT
    list after writing the rule.
    """
    outcome = FlagOutcome()

    index_by_column = {name: position for position, name in enumerate(columns)}
    usable: list[tuple[RuleSpec, list[tuple[ConditionSpec, int]]]] = []

    for rule in rules:
        if not rule.enabled:
            continue

        if not rule.conditions:
            # Should be unreachable through the API, which rejects an empty
            # condition list at validation. Matching nothing is the safe
            # reading of "a rule that asks for nothing".
            outcome.warnings.append(
                f"Rule {rule.name!r} has no conditions and matched nothing."
            )
            outcome.rules.append(
                RuleHit(id=rule.id, name=rule.name, severity=rule.severity, matched=0)
            )
            continue

        resolved: list[tuple[ConditionSpec, int]] = []
        missing: list[str] = []
        for condition in rule.conditions:
            position = index_by_column.get(condition.column_name)
            if position is None:
                missing.append(condition.column_name)
            else:
                resolved.append((condition, position))

        if missing:
            outcome.warnings.append(
                f"Rule {rule.name!r} refers to "
                f"{', '.join(repr(name) for name in sorted(set(missing)))}, "
                f"which the result does not contain "
                f"{sorted(index_by_column)}. It matched nothing."
            )
            outcome.rules.append(
                RuleHit(id=rule.id, name=rule.name, severity=rule.severity, matched=0)
            )
            continue

        usable.append((rule, resolved))

    if not usable:
        return outcome

    matched_counts = {rule.id: 0 for rule, _ in usable}
    flagged: list[RowFlag] = []

    for row_index, row in enumerate(rows):
        hit_ids: list[str] = []
        for rule, resolved in usable:
            if all(
                evaluate_condition(condition, row[position])
                for condition, position in resolved
            ):
                hit_ids.append(rule.id)
                matched_counts[rule.id] += 1
        if hit_ids:
            flagged.append(RowFlag(index=row_index, rule_ids=hit_ids))

    outcome.rows = flagged
    # Keep every rule in the tally, including ones that matched nothing: "this
    # rule caught 0 rows" is information, and a rule vanishing from the summary
    # looks like it was never saved.
    for rule, _ in usable:
        outcome.rules.append(
            RuleHit(
                id=rule.id,
                name=rule.name,
                severity=rule.severity,
                matched=matched_counts[rule.id],
            )
        )
    return outcome


def specs_from_models(rules: Iterable[Any]) -> list[RuleSpec]:
    """Adapt ORM ``FlagRule`` rows into the pure specs this module evaluates.

    Keeping the evaluator free of ORM types is what lets the whole operator
    matrix be tested in the gate lane with no database at all.
    """
    return [
        RuleSpec(
            id=rule.id,
            name=rule.name,
            severity=rule.severity,
            enabled=rule.enabled,
            conditions=tuple(
                ConditionSpec(
                    column_name=condition.column_name,
                    operator=condition.operator,
                    value=condition.value,
                    value2=condition.value2,
                )
                for condition in sorted(rule.conditions, key=lambda c: c.position)
            ),
        )
        for rule in sorted(rules, key=lambda r: r.position)
    ]
