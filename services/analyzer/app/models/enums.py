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


class SslMode(StrEnum):
    """How much TLS a target connection insists on.

    libpq's own vocabulary, so the Postgres mapping is the identity function and
    nobody has to hold a translation table in their head while reading a
    connection row. MySQL has no equivalent parameter and is mapped in
    ``app.db.target_registry.mysql_connect_args``.

    Ordered weakest to strongest. The two ``verify`` modes are the only ones
    that authenticate the server rather than merely encrypting the wire, so they
    are the only ones a root certificate applies to.
    """

    DISABLE = "disable"
    ALLOW = "allow"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


#: Modes that check the server's certificate, and so can read a root cert.
VERIFYING_SSL_MODES = frozenset({SslMode.VERIFY_CA, SslMode.VERIFY_FULL})


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
