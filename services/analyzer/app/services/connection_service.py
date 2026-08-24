"""Connection profile lifecycle: create, test, list, update, delete."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import target_registry
from app.errors import AppError, DuplicateNameError, ErrorCode, NotFoundError
from app.models import Connection, ConnectionStatus, DbType, utcnow
from app.models.enums import VERIFYING_SSL_MODES
from app.schemas.connection import ConnectionCreate, ConnectionUpdate
from app.security.crypto import encrypt

logger = logging.getLogger(__name__)


def get_connection(session: Session, connection_id: str) -> Connection:
    conn = session.get(Connection, connection_id)
    if conn is None:
        raise NotFoundError(
            ErrorCode.CONNECTION_NOT_FOUND,
            f"No connection with id {connection_id!r}.",
            {"connection_id": connection_id},
        )
    return conn


def list_connections(session: Session) -> list[Connection]:
    return list(session.scalars(select(Connection).order_by(Connection.created_at)))


def test_connection(session: Session, conn: Connection) -> AppError | None:
    """Probe the target and record the outcome. Returns the failure, if any."""
    error: AppError | None = None
    try:
        target_registry.probe(conn)
    except AppError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - never let a probe crash the request
        error = target_registry.translate_db_error(exc)

    conn.status = ConnectionStatus.FAILED if error else ConnectionStatus.OK
    conn.last_tested_at = utcnow()
    conn.last_test_error = error.message if error else None
    session.commit()

    if error is not None:
        # A failed probe usually means the cached engine is useless. Dropping it
        # forces a clean reconnect on the next attempt rather than reusing a
        # pool full of dead connections.
        target_registry.dispose_engine(conn.id)
    return error


def create_connection(
    session: Session, payload: ConnectionCreate
) -> tuple[Connection, AppError | None]:
    """Persist a connection and test it immediately.

    A failing test does not prevent the save: the profile is stored with
    ``status=failed`` and the error is returned, so the user can fix the
    credentials without retyping everything.
    """
    conn = Connection(
        name=payload.name,
        db_type=payload.db_type,
        host=payload.host,
        port=payload.port,
        database=payload.database,
        username=payload.username,
        sqlite_path=payload.sqlite_path,
        ssl_mode=payload.ssl_mode,
        ssl_root_cert=payload.ssl_root_cert,
        password_encrypted=(
            encrypt(payload.password.get_secret_value()) if payload.password else None
        ),
        status=ConnectionStatus.UNTESTED,
    )
    session.add(conn)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(
            f"A connection named {payload.name!r} already exists.",
            {"name": payload.name},
        ) from exc

    error = test_connection(session, conn)
    return conn, error


def update_connection(
    session: Session, conn: Connection, payload: ConnectionUpdate
) -> tuple[Connection, AppError | None]:
    """Apply a partial update, then re-test.

    The cached engine is disposed before re-testing, because a changed host or
    password must not keep serving from a pool built with the old credentials.
    """
    data = payload.model_dump(exclude_unset=True)
    password = data.pop("password", None)

    if conn.db_type == DbType.SQLITE and any(
        data.get(field) for field in ("host", "port", "database", "username")
    ):
        raise AppError(
            ErrorCode.INVALID_CONNECTION_CONFIG,
            "host, port, database and username do not apply to a sqlite connection.",
        )
    if conn.db_type != DbType.SQLITE and data.get("sqlite_path"):
        raise AppError(
            ErrorCode.INVALID_CONNECTION_CONFIG,
            "'sqlite_path' is only valid for a sqlite connection.",
        )

    # Checked against the merged state, not the payload. A partial update that
    # sets only a root certificate, or only relaxes the mode, would each pass a
    # payload-local check and still leave a certificate that nothing reads.
    merged_mode = data.get("ssl_mode", conn.ssl_mode)
    merged_cert = data.get("ssl_root_cert", conn.ssl_root_cert)
    if merged_cert and merged_mode not in VERIFYING_SSL_MODES:
        raise AppError(
            ErrorCode.INVALID_CONNECTION_CONFIG,
            "'ssl_root_cert' only applies to the verify-ca and verify-full TLS "
            f"modes, not {merged_mode.value!r}. Clear the certificate or raise "
            "the mode.",
        )

    for field, value in data.items():
        setattr(conn, field, value)
    if password is not None:
        conn.password_encrypted = encrypt(password.get_secret_value())

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(
            f"A connection named {payload.name!r} already exists.",
            {"name": payload.name},
        ) from exc

    target_registry.dispose_engine(conn.id)
    error = test_connection(session, conn)
    return conn, error


def delete_connection(session: Session, conn: Connection) -> None:
    """Delete a connection and, by cascade, its saved queries and their logs.

    Cascade is deliberate and documented: this is a single-tenant tool, so
    there is no other user whose saved work could be destroyed by the delete.
    """
    connection_id = conn.id
    session.delete(conn)
    session.commit()
    target_registry.dispose_engine(connection_id)
