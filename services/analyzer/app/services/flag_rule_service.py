"""Storage and aggregation for flag rules.

Two responsibilities: persist a query's rule set, and gather flagged rows across
every query on a connection.

The aggregate deliberately reads the result cache rather than running anything.
Opening a connection's flagged view would otherwise fire every saved query on
that connection at a customer's production database at once, which is a lot of
load to put behind a page load. Refreshing is a separate, explicit action.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import Connection, FlagCondition, FlagRule, SavedQuery
from app.services import (
    flag_dismissal_service,
    flagged_row_service,
    query_service,
    result_cache,
)
from app.services.flagging import SEVERITY_ORDER
from app.services.saved_query_service import get_query


def list_rules(session: Session, query_id: str) -> list[FlagRule]:
    """Every rule on a query, in display order, with conditions loaded.

    ``selectinload`` on both levels: without it, rendering a rule set of N
    rules costs 1 + N statements, and the editor asks for the whole set on
    every open.
    """
    statement = (
        select(FlagRule)
        .where(FlagRule.query_id == query_id)
        .order_by(FlagRule.position)
        .options(selectinload(FlagRule.conditions))
    )
    return list(session.scalars(statement))


def replace_rules(session: Session, query: SavedQuery, rules: list) -> list[FlagRule]:
    """Replace a query's entire rule set.

    Whole-set replace rather than per-rule CRUD. The editor edits every rule at
    once, position is simply the index in the incoming list, and there is no
    reorder endpoint to keep consistent. The cost is that rule ids are not
    stable across a save; nothing references a rule id across a request
    boundary, and the outcome carries names as well as ids for display.
    """
    # delete-orphan on the relationship does the deletion, so clearing the
    # collection is enough. Assigning a fresh list would leave the old rows
    # orphaned but present until the cascade noticed.
    query.flag_rules.clear()
    session.flush()

    built: list[FlagRule] = []
    for position, spec in enumerate(rules):
        rule = FlagRule(
            query_id=query.id,
            name=spec.name,
            severity=spec.severity,
            enabled=spec.enabled,
            position=position,
            conditions=[
                FlagCondition(
                    position=condition_position,
                    column_name=condition.column_name,
                    operator=condition.operator,
                    value=condition.value,
                    value2=condition.value2,
                )
                for condition_position, condition in enumerate(spec.conditions)
            ],
        )
        query.flag_rules.append(rule)
        built.append(rule)

    session.commit()

    # A rule change alters the payload without altering a single row, so a
    # cached entry would keep serving the old flags and polling would report
    # nothing changed. See canonical_hash in query_service.
    result_cache.invalidate(query.id)

    session.refresh(query, ["flag_rules"])
    return list_rules(session, query.id)


def rules_for_query(session: Session, query_id: str) -> list[FlagRule]:
    """Rules of one query, raising QUERY_NOT_FOUND if the query is gone."""
    get_query(session, query_id)
    return list_rules(session, query_id)


# ---------------------------------------------------------------------------
# Connection-level aggregation
# ---------------------------------------------------------------------------


def queries_with_rules(session: Session, connection_id: str) -> list[SavedQuery]:
    """Saved queries on a connection that actually define rules.

    Eager-loads rules and their conditions in one extra statement each. Reading
    ``query.flag_rules`` off a plain listing would emit one statement per query
    and then one per rule, which on a connection with twenty cards is the
    difference between three statements and sixty.
    """
    statement = (
        select(SavedQuery)
        .where(SavedQuery.connection_id == connection_id)
        .order_by(SavedQuery.created_at)
        .options(selectinload(SavedQuery.flag_rules).selectinload(FlagRule.conditions))
    )
    return [query for query in session.scalars(statement) if query.flag_rules]


def _section(session: Session, query: SavedQuery) -> dict:
    """One query's contribution to a connection's flagged view.

    Read from the stored findings, not from the result cache. The cache expires
    on the poll interval and is empty after a restart, so the view used to say
    "not run yet" about queries that had been flagging rows for days. What is
    stored is what needs review.

    ``columns`` comes off the rows themselves, so a finding still renders under
    the headers it was flagged with even if the SELECT list has changed since.
    """
    stored = flagged_row_service.rows_for_query(session, query.id)
    dismissed = len(flag_dismissal_service.dismissed_fingerprints(session, query.id))

    rules_by_id: dict[str, dict] = {}
    for row in stored:
        for rule_id, name in zip(row.rule_ids, row.rule_names, strict=False):
            entry = rules_by_id.setdefault(
                rule_id,
                {"id": rule_id, "name": name, "severity": row.severity.value, "matched": 0},
            )
            entry["matched"] += 1
            # A rule's severity is a property of the rule; the row carries the
            # worst across its rules, so the strongest wins on a tie.
            if SEVERITY_ORDER[row.severity.value] > SEVERITY_ORDER[entry["severity"]]:
                entry["severity"] = row.severity.value

    # Rules that currently match nothing still belong in the legend: "Large
    # transfer 0" is the answer to "did my rule stop working".
    for rule in query.flag_rules:
        rules_by_id.setdefault(
            rule.id,
            {"id": rule.id, "name": rule.name, "severity": rule.severity.value,
             "matched": 0},
        )

    return {
        "query_id": query.id,
        "query_name": query.name,
        "columns": stored[0].columns if stored else [],
        "rows": [
            {
                "index": position,
                "rule_ids": row.rule_ids,
                "rule_names": row.rule_names,
                "values": row.values,
                "fingerprint": row.row_fingerprint,
                "severity": row.severity.value,
                "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at,
            }
            for position, row in enumerate(stored)
        ],
        "rules": list(rules_by_id.values()),
        "warnings": [],
        "flagged_count": len(stored),
        "dismissed_count": dismissed,
        "executed_at": max((row.last_seen_at for row in stored), default=None),
        # Nothing stored and nothing ever run: the view says so rather than
        # implying the rules matched nothing.
        "stale": not stored and result_cache.get(query.id) is None,
        "error_code": None,
        "error_message": None,
    }


def _run_one(session: Session, query: SavedQuery, conn: Connection) -> tuple:
    """Run one query so its stored findings are current.

    Returns ``(error_code, error_message)``; both None on success. One broken
    query must not empty the whole view - the other cards on this connection
    are fine and their findings still matter - so the failure is reported
    against its own section and the rest are built as usual.
    """
    from app.db.target_registry import translate_db_error
    from app.errors import AppError

    try:
        payload = query_service.run_saved_query(query, conn)
    except AppError as exc:
        return exc.error_code.value, exc.message
    except Exception as exc:  # noqa: BLE001 - normalised below
        translated = translate_db_error(exc)
        return translated.error_code.value, translated.message

    interval = query_service.poll_interval_for(query)
    result_cache.set(
        query.id, payload.data_hash, payload.as_dict(interval), ttl_ms=interval
    )
    flagged_row_service.sync(session, query, payload.columns, payload.rows, payload.flags)
    return None, None


def flagged_for_connection(
    session: Session,
    conn: Connection,
    *,
    refresh: bool = False,
) -> dict:
    """Stored findings across every rule-bearing query on one connection.

    Read from the flagged store, so this costs the target database nothing and
    shows what needs review even after a restart. ``refresh`` re-runs the
    queries first, which is the only path here that touches the target.

    Queries with no rules are skipped entirely rather than reported with zero
    hits: a connection where only two of twenty queries define rules should
    show two sections, not eighteen empty ones.
    """
    settings = get_settings()
    queries = queries_with_rules(session, conn.id)

    truncated = False
    if refresh and len(queries) > settings.flagged_refresh_max_queries:
        # One click must not fan out unbounded against a production database.
        queries = queries[: settings.flagged_refresh_max_queries]
        truncated = True

    payloads = []
    for query in queries:
        error_code = error_message = None
        if refresh:
            error_code, error_message = _run_one(session, query, conn)
        section = _section(session, query)
        section["error_code"] = error_code
        section["error_message"] = error_message
        payloads.append(section)

    return {
        "connection_id": conn.id,
        "queries": payloads,
        "flagged_count": sum(payload["flagged_count"] for payload in payloads),
        "dismissed_count": sum(payload["dismissed_count"] for payload in payloads),
        "refreshed": refresh,
        "refresh_truncated": truncated,
    }
