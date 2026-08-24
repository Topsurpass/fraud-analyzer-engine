"""Enums shared by models and Pydantic schemas."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum


class DbType(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"


class ConnectionStatus(StrEnum):
    UNTESTED = "untested"
    OK = "ok"
    FAILED = "failed"


class ChartType(StrEnum):
    LINE = "line"
    BAR = "bar"
    PIE = "pie"
    NUMBER = "number"
    TABLE = "table"


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


#: Operators that read no ``value`` at all. Anything else requires one.
NULLARY_OPERATORS = frozenset({FlagOperator.IS_NULL, FlagOperator.IS_NOT_NULL})

#: Operators that need both ``value`` and ``value2``.
BINARY_OPERATORS = frozenset({FlagOperator.BETWEEN})


def enum_column(enum_cls: type[StrEnum], length: int = 20) -> Enum:
    """A VARCHAR column that stores an enum's *value*, not its name.

    SQLAlchemy persists ``Enum(SomeEnum)`` by member name by default, so
    ``DbType.SQLITE`` lands in the database as ``"SQLITE"`` while the JSON API
    speaks ``"sqlite"``. Two things break as a result:

    * ``server_default`` is written as a value, so any row created by a
      migration, a seed script, or plain SQL is unreadable by the ORM. It
      raises ``LookupError: 'sqlite' is not among the defined enum values``.
    * Anything reading the database directly, which for this service means a
      frontend pointed at the same Postgres, sees different strings than the
      API returns for the same field.

    ``values_callable`` makes storage and the API agree on one spelling.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )
