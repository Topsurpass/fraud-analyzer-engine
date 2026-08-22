"""Request and response models for connection profiles.

``ConnectionRead`` deliberately does not declare ``password`` or
``password_encrypted``. Credentials cannot leak through a response model that
has no field to put them in, which is a stronger guarantee than remembering to
strip them at each call site.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.models.enums import ConnectionStatus, DbType


class ConnectionBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    db_type: DbType
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    sqlite_path: str | None = None


class ConnectionCreate(ConnectionBase):
    password: SecretStr | None = None

    @model_validator(mode="after")
    def _check_fields_for_db_type(self) -> Self:
        if self.db_type == DbType.SQLITE:
            if not self.sqlite_path:
                raise ValueError("sqlite connections require 'sqlite_path'")
            supplied = [
                field
                for field in ("host", "port", "database", "username")
                if getattr(self, field) is not None
            ]
            if supplied or self.password is not None:
                raise ValueError(
                    "sqlite connections must not set "
                    f"{', '.join(supplied + (['password'] if self.password else []))}"
                )
        else:
            missing = [
                field
                for field in ("host", "database", "username")
                if not getattr(self, field)
            ]
            if missing:
                raise ValueError(
                    f"{self.db_type.value} connections require {', '.join(missing)}"
                )
            if self.sqlite_path:
                raise ValueError("'sqlite_path' is only valid for sqlite connections")
        return self


class ConnectionUpdate(BaseModel):
    """Every field optional. Omitted fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    password: SecretStr | None = None
    sqlite_path: str | None = None


class ConnectionRead(BaseModel):
    """Safe projection of a connection. Has no field capable of holding a secret."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    db_type: DbType
    host: str | None
    port: int | None
    database: str | None
    username: str | None
    sqlite_path: str | None
    status: ConnectionStatus
    last_tested_at: datetime | None
    last_test_error: str | None
    created_at: datetime
    updated_at: datetime


class ConnectionCreateResult(BaseModel):
    """A create always returns the saved profile, successful test or not."""

    connection: ConnectionRead
    test_ok: bool
    test_error: str | None = None
    test_error_code: str | None = None


class ConnectionTestResult(BaseModel):
    connection_id: str
    status: ConnectionStatus
    tested_at: datetime
    ok: bool
    error: str | None = None
    error_code: str | None = None


class TableInfo(BaseModel):
    name: str
    kind: str  # "table" or "view"


class TableList(BaseModel):
    connection_id: str
    tables: list[TableInfo]


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool = False


class ColumnList(BaseModel):
    connection_id: str
    table: str
    columns: list[ColumnInfo]
