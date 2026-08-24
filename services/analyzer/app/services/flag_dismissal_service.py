"""Dismissing flagged rows, and remembering it.

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

from app.models import FlagDismissal, SavedQuery


def row_fingerprint(values: list[Any]) -> str:
    """Stable sha256 over one row's values.

    Serialised the same way :func:`app.services.query_service.canonical_hash`
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


def dismissed_fingerprints(session: Session, query_id: str) -> set[str]:
    """Every fingerprint dismissed on one query.

    A set, and read once per query per request: the flagged view tests every
    flagged row against it, so a list would make that quadratic on exactly the
    queries that matter most -- the ones matching a lot of rows.
    """
    statement = select(FlagDismissal.row_fingerprint).where(
        FlagDismissal.query_id == query_id
    )
    return set(session.scalars(statement))


def dismiss(session: Session, query: SavedQuery, fingerprints: list[str]) -> int:
    """Record dismissals, ignoring any already recorded.

    Returns how many were newly stored. Re-dismissing a row is a no-op rather
    than a conflict: two browser tabs on the same queue is normal, and the
    second one is not an error to report to anybody.
    """
    if not fingerprints:
        return 0

    existing = dismissed_fingerprints(session, query.id)
    fresh = [f for f in dict.fromkeys(fingerprints) if f not in existing]
    for fingerprint in fresh:
        session.add(FlagDismissal(query_id=query.id, row_fingerprint=fingerprint))
    session.commit()
    return len(fresh)


def restore(session: Session, query: SavedQuery, fingerprints: list[str] | None) -> int:
    """Undo dismissals. ``None`` restores every row on the query.

    Returns how many were removed. Without this a mis-click is permanent and
    the row it hid is unreachable -- there is no other view that lists dismissed
    rows, because the engine does not keep the rows.
    """
    statement = delete(FlagDismissal).where(FlagDismissal.query_id == query.id)
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
