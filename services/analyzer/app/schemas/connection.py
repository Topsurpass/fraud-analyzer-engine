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

from app.models.enums import VERIFYING_SSL_MODES, ConnectionStatus, DbType, SslMode
from app.schemas.types import UtcDatetime


class ConnectionBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    db_type: DbType
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    sqlite_path: str | None = None

    # Defaults to require, not to libpq's prefer. A target that will not talk
    # in the clear is the common case for managed Postgres, and a target that
    # tolerates plaintext should not be downgraded to it by omission.
    ssl_mode: SslMode = SslMode.REQUIRE
    ssl_root_cert: str | None = None

    @model_validator(mode="after")
    def _check_root_cert_is_used(self) -> Self:
        # A certificate under a non-verifying mode is never read. Storing one
        # anyway would read as protection that is not happening.
        if self.ssl_root_cert and self.ssl_mode not in VERIFYING_SSL_MODES:
            raise ValueError(
                "'ssl_root_cert' only applies to the verify-ca and verify-full "
                f"TLS modes, not {self.ssl_mode.value!r}"
            )
        return self


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
    ssl_mode: SslMode | None = None
    ssl_root_cert: str | None = None


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
    ssl_mode: SslMode
    ssl_root_cert: str | None
    status: ConnectionStatus
    last_tested_at: UtcDatetime | None
    last_test_error: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ConnectionCreateResult(BaseModel):
    """A create always returns the saved profile, successful test or not."""

    connection: ConnectionRead
    test_ok: bool
    test_error: str | None = None
    test_error_code: str | None = None


class ConnectionTestResult(BaseModel):
    connection_id: str
    status: ConnectionStatus
    tested_at: UtcDatetime
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
