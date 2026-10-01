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

import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.features.connections.models import Connection
from app.features.flag_rules.dismissals import row_fingerprint
from app.features.flag_rules.models import FlaggedRow, FlagDismissal
from app.features.queries import service as saved_query_service
from app.features.queries.models import SavedQuery
from app.features.users.models import User
from app.policy.flag_rules import SEVERITY_ORDER, FlagSeverity

def _worst(severities: list[str]) -> FlagSeverity:
    best = FlagSeverity.LOW
    for name in severities:
        try:
            candidate = FlagSeverity(name)
        except ValueError:  # pragma: no cover - a severity the enum lost
            continue
        if SEVERITY_ORDER[candidate] > SEVERITY_ORDER[best]:
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

    now = utcnow()

    matched: dict[str, dict] = {}
    for flagged in outcome.get("rows") or []:
        index = flagged["index"]
        if not (0 <= index < len(rows)):
            continue
        values = rows[index]
        fingerprint = flagged.get("fingerprint") or row_fingerprint(values)
        # Every current match is stored, dismissed by somebody or not. A
        # dismissal is one person's reading state (see ``dismissals``), applied
        # when findings are read; skipping a row here because one person
        # dismissed it would hide it from everybody else's queue.
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
    stored.sort(key=lambda row: (-SEVERITY_ORDER[row.severity], row.first_seen_at))
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


def summary(session: Session, user: User) -> dict:
    """Flagged totals per connection and per query the caller may see, plus
    when the newest arrived. What the caller has dismissed is not counted.

    Everything the sidebar, the connection list and the notification bell need
    in one request. The
    alternative is a count endpoint per card, which is the same data fetched
    once per thing on screen.

    Filtered by ``alert_visible_to``: this is the badge every page reads on
    load, so an unfiltered count or severity here would leak the existence and
    urgency of another analyst's findings on every navigation. The caller's own
    queries, everything for an administrator, and any query that has been
    published: sharing a chart shares its alerts.
    """
    # The connection's name comes along rather than being looked up by the
    # client. A notification listing "c9a86758" is not a notification, and
    # joining here costs one statement instead of coupling the caller to
    # whatever else happens to have loaded the connection list.
    statement = (
        select(
            SavedQuery.connection_id,
            Connection.name,
            FlaggedRow.query_id,
            FlaggedRow.severity,
            func.count(),
            func.max(FlaggedRow.first_seen_at),
            SavedQuery.owner_id,
        )
        .join(SavedQuery, SavedQuery.id == FlaggedRow.query_id)
        .join(Connection, Connection.id == SavedQuery.connection_id)
        .where(
            saved_query_service.alert_visible_to(user),
            # The caller's own dismissals only: a finding somebody else cleared
            # is still this person's to review.
            ~sa.exists().where(
                FlagDismissal.query_id == FlaggedRow.query_id,
                FlagDismissal.row_fingerprint == FlaggedRow.row_fingerprint,
                FlagDismissal.user_id == user.id,
            ),
        )
        .group_by(
            SavedQuery.connection_id,
            Connection.name,
            FlaggedRow.query_id,
            FlaggedRow.severity,
            SavedQuery.owner_id,
        )
    )

    def _newer(current, candidate):
        if candidate is None:
            return current
        return candidate if current is None or candidate > current else current

    per_query: dict[str, dict] = {}
    per_connection: dict[str, dict] = {}
    newest = None
    for connection_id, name, query_id, severity, total, seen, owner_id in session.execute(
        statement
    ):
        newest = _newer(newest, seen)

        query_entry = per_query.setdefault(
            query_id,
            {"query_id": query_id, "connection_id": connection_id, "flagged_count": 0,
             "severity": FlagSeverity.LOW, "newest_first_seen_at": None,
             # Not the caller's own work: it reached them by being published (or,
             # for an administrator, by being an administrator).
             "shared": owner_id != user.id},
        )
        query_entry["flagged_count"] += total
        query_entry["newest_first_seen_at"] = _newer(
            query_entry["newest_first_seen_at"], seen
        )
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[query_entry["severity"]]:
            query_entry["severity"] = severity

        connection_entry = per_connection.setdefault(
            connection_id,
            {"connection_id": connection_id, "connection_name": name,
             "flagged_count": 0, "severity": FlagSeverity.LOW,
             "newest_first_seen_at": None},
        )
        connection_entry["flagged_count"] += total
        connection_entry["newest_first_seen_at"] = _newer(
            connection_entry["newest_first_seen_at"], seen
        )
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[connection_entry["severity"]]:
            connection_entry["severity"] = severity

    return {
        "connections": sorted(
            per_connection.values(), key=lambda e: -e["flagged_count"]
        ),
        "queries": sorted(per_query.values(), key=lambda e: -e["flagged_count"]),
        "flagged_count": sum(e["flagged_count"] for e in per_connection.values()),
        # When the most recent finding anywhere first appeared. A notification
        # needs this rather than the count: dismiss two and gain two and the
        # count has not moved, but something new has arrived and the reader has
        # not seen it.
        "newest_first_seen_at": newest,
    }
