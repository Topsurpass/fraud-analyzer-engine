"""Keeping the stored flagged rows in step with what the rules currently match.

One entry point matters: :func:`sync`, called after every run of a query that
has rules. It makes the stored set equal the matched set, which is the whole
contract -- the table is a queue of what needs review *now*, not a log of
everything that ever tripped a rule.

Three consequences fall out of that, and each is a deliberate answer to a
question someone will ask:

* A row that stops matching is deleted. The amount was corrected, or the rule
  changed; either way it is no longer a finding and leaving it would make the
  counts describe the past.
* A row that keeps matching is updated, not duplicated, and keeps its original
  ``first_seen_at``. "This has been sitting here for three days" is the fact an
  analyst acts on, and a re-run every poll interval must not reset it.
* A dismissed row is never re-inserted. Without that the queue refills itself
  on the next scheduled run and dismissing means nothing.

Nothing here touches the target database. These are the engine's own rows; the
target connection is opened read-only and is never written to at all.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import FlaggedRow, SavedQuery, utcnow
from app.models.enums import FlagSeverity
from app.services.flag_dismissal_service import (
    dismissed_fingerprints,
    row_fingerprint,
)

#: Ranking for "worst thing in this queue", matching the frontend's order.
_SEVERITY_RANK = {FlagSeverity.LOW: 0, FlagSeverity.MEDIUM: 1, FlagSeverity.HIGH: 2}


def _worst(severities: list[str]) -> FlagSeverity:
    best = FlagSeverity.LOW
    for name in severities:
        try:
            candidate = FlagSeverity(name)
        except ValueError:  # pragma: no cover - a severity the enum lost
            continue
        if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[best]:
            best = candidate
    return best


def sync(
    session: Session,
    query: SavedQuery,
    columns: list[str],
    rows: list[list[Any]],
    outcome: dict,
) -> int:
    """Make the stored flagged rows equal what this run matched.

    Returns the number of rows now stored. Commits, because the caller is a
    request handler or the scheduler and neither should be left holding a
    half-applied queue.

    A query with no rules stores nothing and clears anything it had: removing
    the last rule should empty its section rather than freeze it.
    """
    rule_names = {rule["id"]: rule["name"] for rule in outcome.get("rules") or []}
    rule_severities = {
        rule["id"]: rule["severity"] for rule in outcome.get("rules") or []
    }

    dismissed = dismissed_fingerprints(session, query.id)
    now = utcnow()

    matched: dict[str, dict] = {}
    for flagged in outcome.get("rows") or []:
        index = flagged["index"]
        if not (0 <= index < len(rows)):
            continue
        values = rows[index]
        fingerprint = flagged.get("fingerprint") or row_fingerprint(values)
        if fingerprint in dismissed:
            # Reviewed already. Storing it again is exactly the refill this
            # exists to prevent.
            continue
        ids = list(flagged.get("rule_ids") or ())
        matched[fingerprint] = {
            "values": values,
            "columns": columns,
            "rule_ids": ids,
            "rule_names": [rule_names[i] for i in ids if i in rule_names],
            "severity": _worst([rule_severities[i] for i in ids if i in rule_severities]),
        }

    existing = {
        row.row_fingerprint: row
        for row in session.scalars(
            select(FlaggedRow).where(FlaggedRow.query_id == query.id)
        )
    }

    for fingerprint, row in existing.items():
        found = matched.get(fingerprint)
        if found is None:
            # No longer a finding. See the module docstring: this table is the
            # present, not the history.
            session.delete(row)
            continue
        # Kept: refresh what the rules say about it, but never first_seen_at.
        row.values = found["values"]
        row.columns = found["columns"]
        row.rule_ids = found["rule_ids"]
        row.rule_names = found["rule_names"]
        row.severity = found["severity"]
        row.last_seen_at = now

    for fingerprint, found in matched.items():
        if fingerprint in existing:
            continue
        session.add(
            FlaggedRow(
                query_id=query.id,
                row_fingerprint=fingerprint,
                values=found["values"],
                columns=found["columns"],
                rule_ids=found["rule_ids"],
                rule_names=found["rule_names"],
                severity=found["severity"],
                first_seen_at=now,
                last_seen_at=now,
            )
        )

    session.commit()
    return len(matched)


def rows_for_query(session: Session, query_id: str) -> list[FlaggedRow]:
    """Stored findings for one query, worst first, then oldest first.

    Oldest-first within a severity on purpose: the thing that has been waiting
    longest is the thing most likely to be overdue.
    """
    stored = list(
        session.scalars(select(FlaggedRow).where(FlaggedRow.query_id == query_id))
    )
    stored.sort(key=lambda row: (-_SEVERITY_RANK[row.severity], row.first_seen_at))
    return stored


def delete_rows(
    session: Session, query_id: str, fingerprints: list[str] | None
) -> int:
    """Delete stored findings. ``None`` deletes every one on the query.

    Only ever the engine's own copy. The row in the customer's database is
    untouched and unreachable from here: target connections are opened
    read-only.
    """
    statement = delete(FlaggedRow).where(FlaggedRow.query_id == query_id)
    if fingerprints is not None:
        if not fingerprints:
            return 0
        statement = statement.where(FlaggedRow.row_fingerprint.in_(fingerprints))
    removed = session.execute(statement).rowcount or 0
    session.commit()
    return removed


def summary(session: Session) -> dict:
    """Flagged totals per connection and per query, for the navigation badges.

    Everything the sidebar and the connection list need in one request. The
    alternative is a count endpoint per card, which is the same data fetched
    once per thing on screen.
    """
    statement = (
        select(
            SavedQuery.connection_id,
            FlaggedRow.query_id,
            FlaggedRow.severity,
            func.count(),
        )
        .join(SavedQuery, SavedQuery.id == FlaggedRow.query_id)
        .group_by(SavedQuery.connection_id, FlaggedRow.query_id, FlaggedRow.severity)
    )

    per_query: dict[str, dict] = {}
    per_connection: dict[str, dict] = {}
    for connection_id, query_id, severity, total in session.execute(statement):
        query_entry = per_query.setdefault(
            query_id,
            {"query_id": query_id, "connection_id": connection_id, "flagged_count": 0,
             "severity": FlagSeverity.LOW},
        )
        query_entry["flagged_count"] += total
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[query_entry["severity"]]:
            query_entry["severity"] = severity

        connection_entry = per_connection.setdefault(
            connection_id,
            {"connection_id": connection_id, "flagged_count": 0,
             "severity": FlagSeverity.LOW},
        )
        connection_entry["flagged_count"] += total
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[connection_entry["severity"]]:
            connection_entry["severity"] = severity

    return {
        "connections": sorted(
            per_connection.values(), key=lambda e: -e["flagged_count"]
        ),
        "queries": sorted(per_query.values(), key=lambda e: -e["flagged_count"]),
        "flagged_count": sum(e["flagged_count"] for e in per_connection.values()),
    }
