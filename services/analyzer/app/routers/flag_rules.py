"""Flag-rule storage and the per-connection flagged view."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.schemas.flag_rule import FlagRuleSetRead, FlagRuleSetUpdate
from app.services import connection_service
from app.services import flag_rule_service as svc
from app.services import saved_query_service

query_scoped = APIRouter(prefix="/queries", tags=["flag-rules"])
connection_scoped = APIRouter(prefix="/connections", tags=["flag-rules"])


@query_scoped.get("/{query_id}/flag-rules", response_model=FlagRuleSetRead)
def get_flag_rules(
    query_id: str, session: Session = Depends(get_session)
) -> FlagRuleSetRead:
    """Every rule on a query, in display order."""
    rules = svc.rules_for_query(session, query_id)
    return FlagRuleSetRead(query_id=query_id, rules=rules)


@query_scoped.put("/{query_id}/flag-rules", response_model=FlagRuleSetRead)
def put_flag_rules(
    query_id: str,
    payload: FlagRuleSetUpdate,
    session: Session = Depends(get_session),
) -> FlagRuleSetRead:
    """Replace a query's whole rule set.

    Replace rather than per-rule CRUD because the editor edits the set as a
    unit: ``position`` is just the index in the submitted list, so reordering
    needs no endpoint of its own. Sending an empty list removes every rule.
    """
    query = saved_query_service.get_query(session, query_id)
    rules = svc.replace_rules(session, query, payload.rules)
    return FlagRuleSetRead(query_id=query_id, rules=rules)


@connection_scoped.get("/{connection_id}/flagged")
def get_flagged(connection_id: str, session: Session = Depends(get_session)) -> dict:
    """Flagged rows across every rule-bearing query on this connection.

    Reads cached results and runs nothing, so opening the view costs the
    target database nothing. A query with no cached result is returned marked
    ``stale`` rather than silently omitted, so the UI can say "not run yet"
    instead of implying the rules matched nothing.
    """
    conn = connection_service.get_connection(session, connection_id)
    return svc.flagged_for_connection(session, conn, refresh=False)


@connection_scoped.post("/{connection_id}/flagged/refresh")
def refresh_flagged(
    connection_id: str, session: Session = Depends(get_session)
) -> dict:
    """Re-run this connection's rule-bearing queries, then flag them.

    The only path here that touches the target database. Bounded by
    ``FAE_FLAGGED_REFRESH_MAX_QUERIES`` and counted against the execution rate
    limit, since one click can otherwise fan out across every saved query on
    the connection.
    """
    conn = connection_service.get_connection(session, connection_id)
    return svc.flagged_for_connection(session, conn, refresh=True)
