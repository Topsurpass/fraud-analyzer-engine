"""Writing audit entries.

One function, so every call site records the same shape. The alternative -
each router assembling its own entry - produces a table whose rows cannot be
compared to each other six months later.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.enums import AuditAction
from app.models.user import User

#: Keys stripped from ``detail`` before it is stored, whatever the caller says.
#:
#: An audit table is exactly where a credential must never come to rest, and a
#: password-reset entry is the obvious place somebody later adds "the temporary
#: password we issued" for convenience. Refusing at the one chokepoint is the
#: only version of this rule that holds.
_FORBIDDEN_DETAIL_KEYS = frozenset({"password", "new_password", "temporary_password", "token"})


def record(
    db: Session,
    actor: User,
    action: AuditAction,
    target_type: str,
    target_id: str,
    detail: dict | None = None,
) -> AuditLog:
    """Append one entry. Commits, because an audit write must not be rolled
    back by a later failure in the operation it describes."""
    safe = (
        {k: v for k, v in detail.items() if k not in _FORBIDDEN_DETAIL_KEYS}
        if detail
        else None
    )

    entry = AuditLog(
        actor_id=actor.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=safe or None,
    )
    db.add(entry)
    db.commit()
    return entry
