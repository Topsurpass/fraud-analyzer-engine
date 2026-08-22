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
