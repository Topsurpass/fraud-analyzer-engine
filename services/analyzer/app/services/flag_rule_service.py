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
from app.services import flag_dismissal_service, flagging, query_service, result_cache
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


class FlaggedQuery:
    """One query's contribution to a connection's flagged view."""

    def __init__(
        self,
        query: SavedQuery,
        *,
        columns: list[str] | None = None,
        rows: list[list] | None = None,
        outcome: dict | None = None,
        executed_at: datetime | None = None,
        stale: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        dismissed: set[str] | None = None,
    ) -> None:
        self.query = query
        self.columns = columns or []
        self.rows = rows or []
        self.outcome = outcome or flagging.FlagOutcome().as_dict()
        self.executed_at = executed_at
        self.stale = stale
        self.error_code = error_code
        self.error_message = error_message
        self.dismissed = dismissed or set()

    def as_dict(self) -> dict:
        """Only the flagged rows themselves, not the whole result.

        The flagged view exists to show what matched. Carrying every row of
        every query on the connection so the client can filter would multiply
        the payload by the inverse of the flag rate, which for a working rule
        set is a large number.

        Rows the analyst has dismissed are removed here rather than at the
        client, so a reviewed row never reaches the browser again. Each row
        carries its fingerprint, which is what the client sends back to dismiss
        it -- the row index cannot serve: it is a position in one run's result
        and means something different after the next.

        ``flagged_count`` counts what is being shown. The number beside a
        section is what the analyst still has to work through, not a total that
        never moves however much of the queue they clear; ``dismissed_count``
        carries the rest, so the view can offer to restore them.
        """
        by_index = {row["index"]: row["rule_ids"] for row in self.outcome["rows"]}

        rows = []
        dismissed_count = 0
        for index, rule_ids in sorted(by_index.items()):
            if index >= len(self.rows):
                continue
            values = self.rows[index]
            fingerprint = flag_dismissal_service.row_fingerprint(values)
            if fingerprint in self.dismissed:
                dismissed_count += 1
                continue
            rows.append(
                {
                    "index": index,
                    "rule_ids": rule_ids,
                    "values": values,
                    "fingerprint": fingerprint,
                }
            )

        # Per-rule counts are recomputed over what survived, for the same
        # reason: a legend reading "Large transfer 40" beside four visible rows
        # describes a queue the analyst has already emptied.
        shown = [set(row["rule_ids"]) for row in rows]
        rules = [
            {**rule, "matched": sum(1 for ids in shown if rule["id"] in ids)}
            for rule in self.outcome["rules"]
        ]

        return {
            "query_id": self.query.id,
            "query_name": self.query.name,
            "columns": self.columns,
            "rows": rows,
            "rules": rules,
            "warnings": self.outcome["warnings"],
            "flagged_count": len(rows),
            "dismissed_count": dismissed_count,
            "executed_at": self.executed_at,
            "stale": self.stale,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _from_cache(query: SavedQuery, dismissed: set[str]) -> FlaggedQuery:
    entry = result_cache.get(query.id)
    if entry is None:
        # Never run, or the entry aged out. Not an error: the view says so and
        # offers a refresh rather than quietly showing nothing.
        return FlaggedQuery(query, stale=True, dismissed=dismissed)

    payload = entry.payload
    return FlaggedQuery(
        query,
        columns=payload.get("columns", []),
        rows=payload.get("rows", []),
        outcome=payload.get("flags") or flagging.FlagOutcome().as_dict(),
        executed_at=payload.get("executed_at"),
        stale=False,
        dismissed=dismissed,
    )


def _run_one(
    session: Session, query: SavedQuery, conn: Connection, dismissed: set[str]
) -> FlaggedQuery:
    from app.db.target_registry import translate_db_error
    from app.errors import AppError

    try:
        payload = query_service.run_saved_query(query, conn)
    except AppError as exc:
        # One broken query must not empty the whole view. The other cards on
        # this connection are fine and their flagged rows still matter.
        return FlaggedQuery(
            query,
            stale=True,
            error_code=exc.error_code.value,
            error_message=exc.message,
            dismissed=dismissed,
        )
    except Exception as exc:  # noqa: BLE001 - normalised below
        translated = translate_db_error(exc)
        return FlaggedQuery(
            query,
            stale=True,
            error_code=translated.error_code.value,
            error_message=translated.message,
            dismissed=dismissed,
        )

    result_cache.set(
        query.id,
        payload.data_hash,
        payload.as_dict(query_service.poll_interval_for(query)),
        ttl_ms=query_service.poll_interval_for(query),
    )
    return FlaggedQuery(
        query,
        columns=payload.columns,
        rows=payload.rows,
        outcome=payload.flags,
        executed_at=payload.executed_at,
        stale=False,
        dismissed=dismissed,
    )


def flagged_for_connection(
    session: Session,
    conn: Connection,
    *,
    refresh: bool = False,
) -> dict:
    """Flagged rows across every rule-bearing query on one connection.

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

    sections = []
    for query in queries:
        dismissed = flag_dismissal_service.dismissed_fingerprints(session, query.id)
        sections.append(
            _run_one(session, query, conn, dismissed)
            if refresh
            else _from_cache(query, dismissed)
        )

    # Built once: as_dict does the dismissal filtering, and the totals have to
    # agree with the sections the client is actually shown.
    payloads = [section.as_dict() for section in sections]

    return {
        "connection_id": conn.id,
        "queries": payloads,
        "flagged_count": sum(payload["flagged_count"] for payload in payloads),
        "dismissed_count": sum(payload["dismissed_count"] for payload in payloads),
        "refreshed": refresh,
        "refresh_truncated": truncated,
    }
