"""Flag-rule storage and the per-connection flagged view."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.schemas.flag_rule import (
    FlagDismissalRequest,
    FlagDismissalResult,
    ConnectionFlaggedRead,
    FlaggedSummaryRead,
    FlagRuleSetRead,
    FlagRuleSetUpdate,
)
from app.security.deps import require_user
from app.services import connection_service
from app.services import flag_dismissal_service as dismissals
from app.services import flagged_row_service as flagged_rows
from app.services import flag_rule_service as svc
from app.services import saved_query_service

query_scoped = APIRouter(
    prefix="/queries", tags=["flag-rules"], dependencies=[Depends(require_user)]
)
connection_scoped = APIRouter(
    prefix="/connections", tags=["flag-rules"], dependencies=[Depends(require_user)]
)
# Its own prefix rather than nested under /connections: the summary spans every
# connection, so hanging it off one connection's path would be a lie.
summary_scoped = APIRouter(
    prefix="/flagged", tags=["flag-rules"], dependencies=[Depends(require_user)]
)


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


@connection_scoped.get("/{connection_id}/flagged", response_model=ConnectionFlaggedRead)
def get_flagged(connection_id: str, session: Session = Depends(get_session)) -> dict:
    """Flagged rows across every rule-bearing query on this connection.

    Reads cached results and runs nothing, so opening the view costs the
    target database nothing. A query with no cached result is returned marked
    ``stale`` rather than silently omitted, so the UI can say "not run yet"
    instead of implying the rules matched nothing.
    """
    conn = connection_service.get_connection(session, connection_id)
    return svc.flagged_for_connection(session, conn, refresh=False)


@connection_scoped.post(
    "/{connection_id}/flagged/refresh", response_model=ConnectionFlaggedRead
)
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


@query_scoped.post("/{query_id}/flag-dismissals", response_model=FlagDismissalResult)
def dismiss_flagged_rows(
    query_id: str,
    payload: FlagDismissalRequest,
    session: Session = Depends(get_session),
) -> FlagDismissalResult:
    """Mark flagged rows as reviewed so they stop appearing.

    Addressed by fingerprint, not by row index: an index is a position in one
    run's result and points somewhere else after the next run. The fingerprints
    come from the flagged view's own rows.

    Dismissing a row that is already dismissed is a no-op rather than a
    conflict -- two tabs open on the same queue is normal use.
    """
    query = saved_query_service.get_query(session, query_id)
    stored = dismissals.dismiss(session, query, payload.fingerprints)
    # Delete the engine's stored copy as well, so the queue actually shrinks
    # rather than being filtered on the way out. The row in the customer's
    # database is untouched: target connections are opened read-only and this
    # is the engine's own bookkeeping table.
    flagged_rows.delete_rows(session, query_id, payload.fingerprints)
    return FlagDismissalResult(query_id=query_id, changed=stored)


@query_scoped.delete("/{query_id}/flag-dismissals", response_model=FlagDismissalResult)
def restore_flagged_rows(
    query_id: str,
    fingerprint: list[str] | None = Query(default=None),
    session: Session = Depends(get_session),
) -> FlagDismissalResult:
    """Undo dismissals: the named rows, or all of them when none are named.

    Without this a mis-click is permanent. Dismissed rows are listed nowhere --
    the engine stores their hashes, not the rows -- so there is no other way
    back to one.
    """
    query = saved_query_service.get_query(session, query_id)
    removed = dismissals.restore(session, query, fingerprint)
    return FlagDismissalResult(query_id=query_id, changed=removed)


@query_scoped.delete("/{query_id}/flagged-rows", response_model=FlagDismissalResult)
def delete_flagged_rows(
    query_id: str,
    fingerprint: list[str] | None = Query(default=None),
    session: Session = Depends(get_session),
) -> FlagDismissalResult:
    """Delete stored findings without recording a dismissal.

    Separate from dismissing on purpose. Dismissing says "reviewed, do not show
    me this again" and is remembered, so the next scheduled run will not
    re-flag the row. Deleting only clears what is stored now; if the row still
    matches when the query next runs it comes back. Use this to clear a queue
    after changing a rule, when the old findings are noise rather than
    decisions.

    Either way this only ever removes the engine's own copy.
    """
    saved_query_service.get_query(session, query_id)
    removed = flagged_rows.delete_rows(session, query_id, fingerprint)
    return FlagDismissalResult(query_id=query_id, changed=removed)


@summary_scoped.get("/summary", response_model=FlaggedSummaryRead)
def flagged_summary(session: Session = Depends(get_session)) -> dict:
    """Flagged totals per connection and per query, in one request.

    What the navigation badges need. A count endpoint per card would be the
    same data fetched once per thing on screen, and this is read on every page.
    """
    return flagged_rows.summary(session)
