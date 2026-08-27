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


def record(
    db: Session,
    actor: User,
    action: AuditAction,
    target_type: str,
    target_id: str,
    detail: dict | None = None,
) -> AuditLog:
    """Append one entry. Commits, because an audit write must not be rolled
    back by a later failure in the operation it describes.

    ``detail`` is passed through unscrubbed on purpose: ``AuditLog``'s own
    ``@validates("detail")`` hook (see ``app.models.audit_log``) is what
    actually strips credential-shaped keys, so the guarantee holds for every
    way a row gets built, not just this function. Filtering again here would
    be a second copy of the same rule that can quietly drift from the first.
    """
    entry = AuditLog(
        actor_id=actor.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    )
    db.add(entry)
    db.commit()
    return entry
