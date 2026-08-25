"""Connection profile endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.models import utcnow
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionCreateResult,
    ConnectionRead,
    ConnectionTestResult,
    ConnectionUpdate,
)
from app.services import connection_service

router = APIRouter(prefix="/connections", tags=["connections"])


@router.post("", response_model=ConnectionCreateResult, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: ConnectionCreate, session: Session = Depends(get_session)
) -> ConnectionCreateResult:
    """Create a connection profile and test it immediately.

    A failing test still saves the profile, with ``status="failed"`` and the
    error attached, so credentials can be corrected without re-entering
    everything.
    """
    conn, error = connection_service.create_connection(session, payload)
    return ConnectionCreateResult(
        connection=ConnectionRead.model_validate(conn),
        test_ok=error is None,
        test_error=error.message if error else None,
        test_error_code=error.error_code.value if error else None,
    )


@router.get("", response_model=list[ConnectionRead])
def list_connections(session: Session = Depends(get_session)) -> list[ConnectionRead]:
    """List every connection. Credentials are never included."""
    return [
        ConnectionRead.model_validate(c)
        for c in connection_service.list_connections(session)
    ]


@router.get("/{connection_id}", response_model=ConnectionRead)
def get_connection(
    connection_id: str, session: Session = Depends(get_session)
) -> ConnectionRead:
    """Fetch one connection. Credentials are never included."""
    return ConnectionRead.model_validate(
        connection_service.get_connection(session, connection_id)
    )


@router.put("/{connection_id}", response_model=ConnectionCreateResult)
def update_connection(
    connection_id: str,
    payload: ConnectionUpdate,
    session: Session = Depends(get_session),
) -> ConnectionCreateResult:
    """Partially update a connection, then re-test it."""
    conn = connection_service.get_connection(session, connection_id)
    conn, error = connection_service.update_connection(session, conn, payload)
    return ConnectionCreateResult(
        connection=ConnectionRead.model_validate(conn),
        test_ok=error is None,
        test_error=error.message if error else None,
        test_error_code=error.error_code.value if error else None,
    )


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(
    connection_id: str, session: Session = Depends(get_session)
) -> ConnectionTestResult:
    """Re-test an existing connection and update its status."""
    conn = connection_service.get_connection(session, connection_id)
    error = connection_service.test_connection(session, conn)
    return ConnectionTestResult(
        connection_id=conn.id,
        status=conn.status,
        tested_at=conn.last_tested_at or utcnow(),
        ok=error is None,
        error=error.message if error else None,
        error_code=error.error_code.value if error else None,
    )


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: str, session: Session = Depends(get_session)
) -> Response:
    """Delete a connection.

    This cascades: every saved query on the connection and every execution log
    row for those queries is deleted with it.
    """
    conn = connection_service.get_connection(session, connection_id)
    connection_service.delete_connection(session, conn)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{connection_id}/disconnect", response_model=ConnectionRead)
def disconnect_connection(
    connection_id: str, session: Session = Depends(get_session)
) -> Connection:
    """Stop using this connection until it is reconnected.

    Closes its pooled connections immediately, so the target database sees them
    go away rather than merely being ignored, and stops the scheduler running
    its queries. Everything else is kept: saved queries, flag rules, and every
    flagged row already found.

    Runs against a disconnected connection are refused with CONNECTION_PAUSED
    rather than silently reconnecting - "disconnected" that reconnects itself
    on the next poll would not be worth having.
    """
    conn = connection_service.get_connection(session, connection_id)
    return connection_service.pause_connection(session, conn)


@router.post("/{connection_id}/reconnect", response_model=ConnectionTestResult)
def reconnect_connection(
    connection_id: str, session: Session = Depends(get_session)
) -> ConnectionTestResult:
    """Put a disconnected connection back into service, and test it.

    Tested rather than trusted: one paused for a week may have had its password
    rotated or its host moved, and reporting "connected" without checking only
    moves the failure to the next scheduled run, where nobody is watching.
    """
    conn = connection_service.get_connection(session, connection_id)
    conn, error = connection_service.resume_connection(session, conn)
    return ConnectionTestResult(
        connection_id=conn.id,
        status=conn.status,
        tested_at=conn.last_tested_at or utcnow(),
        ok=error is None,
        error=error.message if error else None,
        error_code=error.error_code.value if error else None,
    )
