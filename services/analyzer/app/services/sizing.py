"""Cheap size estimation for JSON-coerced payloads.

Shared by the execution path, which uses it to refuse a runaway result before
materialising it, and by the result cache, which uses it to bound memory. It
lives on its own so the cache does not have to import a service to measure
what it is holding.

Deliberately an estimate. Both callers need a budget, not an invoice, and
paying a real ``json.dumps`` to measure would cost more than the budget saves.
"""

from __future__ import annotations

from typing import Any

#: Rough per-cell cost of a non-string value once serialised, plus the comma
#: and quoting JSON adds.
SCALAR_COST = 12


def approx_json_size(value: Any) -> int:
    """Estimate a coerced value's serialised size, in bytes."""
    if isinstance(value, str):
        return len(value) + 2
    if isinstance(value, (list, tuple)):
        return sum(approx_json_size(item) for item in value) + 2
    if isinstance(value, dict):
        return sum(len(str(k)) + approx_json_size(v) + 4 for k, v in value.items()) + 2
    return SCALAR_COST
