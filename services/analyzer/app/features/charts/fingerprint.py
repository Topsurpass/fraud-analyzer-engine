"""A hash of everything an administrator reviews when they approve a chart.

Approval is bound to a definition, not to a chart. Without this, an author could
request, let the administrator open the definition, withdraw (which unfreezes the
query), change the SQL, and request again; the administrator, still looking at the
page they opened, would approve SQL nobody had read. The fingerprint is shown with
the definition, and ``approve`` refuses (``DEFINITION_CHANGED``) when the
definition it describes is not the one that is stored now.

Covered: the query's ``sql_text``, ``row_limit`` and ``poll_interval_ms`` as stored,
the chart's type and field mapping, and every rule in position order with each
condition's column, operator, values and list id.

Not covered, by design of what a definition is: the chart's name and position (they
do not change what runs or flags), and **a list's items**. Lists are shared and their
creator or an administrator can edit them, which changes what a published rule flags
without changing the definition. That is a known limit, documented in
``contracts/analyzer-api.md``; closing it means versioning lists into the hash.

Stored values rather than effective ones: ``None`` for a limit the author left unset
stays ``None``, so a later change to the app-wide default does not read as the author
editing the query, and an author who sets a limit explicitly to today's default does
change the fingerprint, which errs towards asking the administrator again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

#: Bumped if the canonical form ever changes, so old and new hashes cannot collide
#: by accident. Bumping it makes every pending request ask for a fresh review,
#: which is the safe direction.
VERSION = 1


def _plain(value: Any) -> Any:
    """An enum's value, anything else as it is."""
    return getattr(value, "value", value)


def definition_fingerprint(query: Any, chart: Any, rules: Iterable[Any]) -> str:
    """sha256 hex over the canonical JSON of one chart's whole definition.

    ``rules`` and each rule's ``conditions`` are taken in ``position`` order, so
    reordering rules changes the fingerprint: rule order is part of what was reviewed.
    """
    canonical = {
        "v": VERSION,
        "sql_text": query.sql_text,
        "row_limit": query.row_limit,
        "poll_interval_ms": query.poll_interval_ms,
        "chart": {
            "chart_type": _plain(chart.chart_type),
            "x_field": chart.x_field,
            "y_field": chart.y_field,
            "series_field": chart.series_field,
            "surge_threshold_pct": chart.surge_threshold_pct,
        },
        "rules": [
            {
                "name": rule.name,
                "severity": _plain(rule.severity),
                "enabled": bool(rule.enabled),
                "conditions": [
                    {
                        "column_name": condition.column_name,
                        "operator": _plain(condition.operator),
                        "value": condition.value,
                        "value2": condition.value2,
                        "list_id": condition.list_id,
                    }
                    for condition in sorted(rule.conditions, key=lambda c: c.position)
                ],
            }
            for rule in sorted(rules, key=lambda r: r.position)
        ],
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
