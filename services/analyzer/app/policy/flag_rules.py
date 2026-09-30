"""What a flag rule may say: its severities and the comparisons a condition can make.

EDIT THIS FILE to add or remove a severity or a comparison operator.

* A new severity: add the member here and give it a rank in ``SEVERITY_ORDER``.
  Stored as a string, so no migration is needed by itself.
* A new operator: add the member here, add it to ``NULLARY_OPERATORS`` or
  ``BINARY_OPERATORS`` if it takes no value or two (or ``LIST_OPERATORS`` if it
  compares against a named list instead of a typed value), then teach
  ``app/features/flag_rules/engine.py:evaluate_condition`` to evaluate it.
  ``tests/test_policy.py`` fails until every operator has an evaluator.
"""

from __future__ import annotations

from enum import StrEnum


class FlagSeverity(StrEnum):
    """How loudly a matched rule should be presented. Ordered low to high."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FlagOperator(StrEnum):
    """Comparisons a flag condition can make against one result column.

    Deliberately a closed set rather than a mini expression language. Every
    member is evaluated in Python against rows the database already returned,
    so nothing here is ever spliced into SQL and none of it can widen what
    ``app.security.sql_guard`` allows through.
    """

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    NEQ = "neq"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    BETWEEN = "between"
    IN_LIST = "in_list"
    NOT_IN_LIST = "not_in_list"


#: Operators that read no ``value`` at all. Anything else requires one.
NULLARY_OPERATORS = frozenset({FlagOperator.IS_NULL, FlagOperator.IS_NOT_NULL})


#: Operators that need both ``value`` and ``value2``.
BINARY_OPERATORS = frozenset({FlagOperator.BETWEEN})


#: Operators that compare against a named list (``list_id``) instead of
#: ``value``. They read neither ``value`` nor ``value2``.
LIST_OPERATORS = frozenset({FlagOperator.IN_LIST, FlagOperator.NOT_IN_LIST})


#: Low to high. Callers sort and group by this rather than by the enum's
#: declaration order, which is not a promise the language makes.
SEVERITY_ORDER: dict[FlagSeverity, int] = {
    FlagSeverity.LOW: 0,
    FlagSeverity.MEDIUM: 1,
    FlagSeverity.HIGH: 2,
}
