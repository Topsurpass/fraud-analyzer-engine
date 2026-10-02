"""Dismissing flagged rows, and remembering it.

A dismissal belongs to the person who made it. Findings are stored once per
query and are visible to everyone who can see a published chart on it, so a
shared dismissal would let a viewer hide the author's findings and the author
hide the viewer's. Each person's queue is the stored findings minus their own
dismissals, nothing else.

The flagged view is a review queue: an analyst works down it and needs the rows
they have cleared to stop coming back. Nothing about a flagged row is stored,
though -- rows are recomputed from the query's cached result on every load -- so
there is no identity to attach "reviewed" to.

:func:`row_fingerprint` supplies one, hashing the row's values as they appear on
the wire. That is the whole design in one function, and its exactness is the
point: a row whose values change no longer matches its dismissal and returns to
the queue. Dismissing says "I looked at this exact row", never "mute this
account".
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.features.flag_rules.models import FlaggedRow, FlagDismissal
from app.features.queries.models import SavedQuery
from app.features.users.models import User


def row_fingerprint(values: list[Any]) -> str:
    """Stable sha256 over one row's values.

    Serialised the same way :func:`app.features.queries.execution.canonical_hash`
    serialises the payload, and fed the values that function already coerced
    through ``to_jsonable``. Both sides of a dismissal -- the one that writes
    the hash and the one that filters on it -- go through here, so there is one
    definition of "the same row" rather than two that can drift.

    No column names, deliberately. Renaming a column in the SELECT list does not
    change which row an analyst reviewed, and folding the names in would clear
    every dismissal on the query the first time someone added an alias.
    """
    canonical = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def dismissed_fingerprints(session: Session, query_id: str, user_id: str) -> set[str]:
    """Every fingerprint this user has dismissed on one query.

    A set, and read once per query per request: the flagged view tests every
    flagged row against it, so a list would make that quadratic on exactly the
    queries that matter most -- the ones matching a lot of rows.
    """
    statement = select(FlagDismissal.row_fingerprint).where(
        FlagDismissal.query_id == query_id, FlagDismissal.user_id == user_id
    )
    return set(session.scalars(statement))


def dismiss(
    session: Session, query: SavedQuery, user: User, fingerprints: list[str]
) -> int:
    """Record the caller's dismissals, ignoring any already recorded.

    Returns how many were newly stored. Re-dismissing a row is a no-op rather
    than a conflict: two browser tabs on the same queue is normal, and the
    second one is not an error to report to anybody.

    Only fingerprints that are stored findings of this query are recorded. A
    dismissal is a row in a table, and any signed-in user may now dismiss on any
    published query, so accepting arbitrary hashes would let one request grow it
    without limit. A fingerprint the engine does not hold (invented, stale after
    the finding stopped matching, or from another query) is ignored and not
    counted. ``restore`` does not need this: it only ever removes.

    Stores the dismissal and nothing else. The finding itself stays, because
    other people's queues are made of it.
    """
    wanted = list(dict.fromkeys(fingerprints))
    if not wanted:
        return 0

    stored = set(
        session.scalars(
            select(FlaggedRow.row_fingerprint).where(
                FlaggedRow.query_id == query.id, FlaggedRow.row_fingerprint.in_(wanted)
            )
        )
    )
    existing = dismissed_fingerprints(session, query.id, user.id)
    fresh = [f for f in wanted if f in stored and f not in existing]
    for fingerprint in fresh:
        session.add(
            FlagDismissal(query_id=query.id, user_id=user.id, row_fingerprint=fingerprint)
        )
    session.commit()
    return len(fresh)


def restore(
    session: Session, query: SavedQuery, user: User, fingerprints: list[str] | None
) -> int:
    """Undo the caller's dismissals. ``None`` restores every row on the query.

    Returns how many were removed. Only ever the caller's own: restoring does
    not bring a finding back for anybody else, because it never left for them.
    Without this a mis-click is permanent and the row it hid is unreachable.
    """
    statement = delete(FlagDismissal).where(
        FlagDismissal.query_id == query.id, FlagDismissal.user_id == user.id
    )
    if fingerprints is not None:
        if not fingerprints:
            return 0
        statement = statement.where(FlagDismissal.row_fingerprint.in_(fingerprints))

    removed = session.execute(statement).rowcount or 0
    session.commit()
    return removed


def apply_dismissals(payload: dict, dismissed: set[str]) -> dict:
    """Remove dismissed rows from a run payload's flag outcome.

    Applied when a payload is *served*, not when it is produced, and the cached
    copy is deliberately left unfiltered. Dismissing on the flagged page would
    otherwise have to invalidate the cache, and the flagged page reads that
    cache: clearing one row would blank the whole section until someone hit
    refresh. Filtering on the way out keeps a dismissal free of the target
    database.

    ``data_hash`` is mixed with the dismissal state, which is the part that is
    easy to miss. A dashboard card polls by asking "anything changed since this
    hash". Dismissing a row changes what the card should draw without changing
    a single value in the result, so a hash over the data alone would answer
    "unchanged" and the row would stay marked on the chart until something else
    happened to move it.

    A payload with nothing dismissed is returned untouched, hash included, so
    this cannot perturb the overwhelmingly common case.
    """
    flags = payload.get("flags") or {}
    rows = flags.get("rows") or []
    if not dismissed or not rows:
        return payload

    kept = [row for row in rows if row.get("fingerprint") not in dismissed]
    if len(kept) == len(rows):
        return payload

    surviving = [set(row.get("rule_ids") or ()) for row in kept]
    return {
        **payload,
        # Suffixed rather than recomputed: the underlying result is unchanged,
        # and rehashing it here would mean holding the rows to do it.
        "data_hash": f"{payload['data_hash']}+d{_stamp(dismissed)}",
        "flags": {
            **flags,
            "rows": kept,
            "flagged_count": len(kept),
            "dismissed_count": len(rows) - len(kept),
            "rules": [
                {**rule, "matched": sum(1 for ids in surviving if rule["id"] in ids)}
                for rule in flags.get("rules") or []
            ],
        },
    }


def _stamp(dismissed: set[str]) -> str:
    """A short, stable digest of a dismissal set, for mixing into a hash."""
    joined = ",".join(sorted(dismissed)).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:16]
