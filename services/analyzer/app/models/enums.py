"""Enums shared by models and Pydantic schemas."""

from __future__ import annotations

from enum import StrEnum


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
