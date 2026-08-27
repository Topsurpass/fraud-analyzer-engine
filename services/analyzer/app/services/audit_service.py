"""Writing audit entries.

One function, so every call site records the same shape. The alternative -
each router assembling its own entry - produces a table whose rows cannot be
compared to each other six months later.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog, scrub_detail
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

    Calls ``scrub_detail`` directly rather than relying only on
    ``AuditLog``'s ``@validates("detail")`` hook to do it implicitly: the two
    call the same function (see ``app.models.audit_log.scrub_detail`` for the
    one definition of "safe"), so there is no duplicated filtering logic to
    drift out of sync, but calling it here too makes the safety property
    visible and testable at the API every current caller actually uses,
    instead of living only in a validator on a model three files away that a
    future author reading this function might not know exists.

    Neither this call nor the ``@validates`` hook covers every way a row can
    be written. SQLAlchemy Core's ``insert(AuditLog.__table__)`` and the
    ``Session`` bulk helpers (``bulk_insert_mappings``, ``bulk_save_objects``)
    write columns directly and never run through this function or through
    attribute assignment on an ``AuditLog`` instance, so neither layer fires
    for them. No call site in this codebase uses those APIs today. Anything
    that starts to -- a backfill, a bulk import -- must call
    ``scrub_detail(detail)`` itself before handing the result to Core or a
    bulk helper; nothing else will do it for it.
    """
    entry = AuditLog(
        actor_id=actor.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=scrub_detail(detail),
    )
    db.add(entry)
    db.commit()
    return entry
