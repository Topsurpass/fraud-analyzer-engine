"""An append-only record of who changed the system.

Distinct from ``query_execution_logs``, which records queries *running*.
Conflating "who granted this person access" with "what ran against the
customer's database" would make both harder to read, and they are consulted by
different people answering different questions.

Nothing here is ever updated or deleted by application code. An audit trail
that its own service can rewrite is not an audit trail.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, validates
from sqlalchemy.types import JSON

from app.models.base import Base, UTCDateTime, new_id, utcnow
from app.models.enums import AuditAction, enum_column

#: Keys stripped from ``detail`` before it is stored, whatever the caller
#: says and however deep in the structure they appear.
#:
#: An audit table is exactly where a credential must never come to rest, and
#: a password-reset entry is the obvious place somebody later adds "the
#: temporary password we issued" for convenience -- ``temp_password`` is here
#: for exactly that reason: ``User.temp_password_expires_at`` and
#: ``generate_temporary_password()`` already name the concept elsewhere in
#: this codebase, which makes ``temp_password`` the single most likely key a
#: future call site reaches for. Matched case-insensitively and after
#: stripping surrounding whitespace, because a second author writing
#: ``Password``, ``PASSWORD``, or ``"  password  "`` off a copy-pasted or
#: templated payload is the median way this gets named, not an adversarial
#: edge case.
FORBIDDEN_DETAIL_KEYS = frozenset(
    {"password", "new_password", "temporary_password", "temp_password", "token", "secret"}
)


def scrub_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip credential-shaped keys from ``detail``, at any depth.

    Recurses into nested dicts and lists so a caller cannot dodge the filter
    by nesting the payload one level down (``{"user": {"password": ...}}``)
    or putting it in a list of records (``{"users": [{"password": ...}]}``) --
    both are ordinary shapes for "what changed", not contrived attacks.

    Collapsing an empty result to ``None`` (rather than storing ``{}``) is
    deliberate: if stripping consumed everything the caller supplied, the
    detail had nothing left worth keeping, and no detail is more honest than
    an empty object that looks like someone meant to record nothing.

    This is the single definition of "safe", called from two places that
    catch two different ways a row gets built: ``audit_service.record``
    calls it directly, and ``AuditLog``'s ``@validates("detail")`` hook below
    calls it again for anything constructed without going through
    ``record`` (see that hook's docstring for what it does and does not
    cover). Two callers sharing one function cannot drift from each other
    the way two independent copies of the filter logic could.

    Neither caller is reached by every way a row can land in this table.
    SQLAlchemy Core's ``insert(AuditLog.__table__)`` and the bulk helpers
    (``Session.bulk_insert_mappings``, ``bulk_save_objects``) write columns
    directly against the table and never construct an ``AuditLog`` Python
    object, so they never trigger ``@validates`` and never run through
    ``record``. No call site in this codebase uses either path today
    (checked), but a future backfill or bulk import that does must call
    ``scrub_detail`` itself before handing ``detail`` to one of those APIs --
    this function is exported (no leading underscore) specifically so it can.
    """

    def _scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _scrub(val)
                for key, val in value.items()
                if key.strip().lower() not in FORBIDDEN_DETAIL_KEYS
            }
        if isinstance(value, list):
            return [_scrub(item) for item in value]
        return value

    if not detail:
        return None
    return _scrub(detail) or None


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    #: Who did it. RESTRICT rather than SET NULL: accounts are never deleted,
    #: and an audit entry that has forgotten its actor answers nothing.
    actor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    action: Mapped[AuditAction] = mapped_column(enum_column(AuditAction, length=40), nullable=False)

    #: What kind of thing was acted on, and which one. Free-form rather than a
    #: foreign key because the target may one day be a connection or a chart,
    #: and an audit row must survive its target being removed.
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    #: ``server_default=func.now()``, matching ``TimestampMixin``'s pattern
    #: elsewhere in this schema: a row inserted by anything other than this
    #: ORM (a migration backfill, a manual SQL fix) still gets a timestamp
    #: rather than a NULL that violates the NOT NULL constraint it was
    #: written under. No ``onupdate`` -- unlike ``TimestampMixin``, this table
    #: has no ``updated_at`` at all, because a row here is never updated.
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=func.now()
    )

    @validates("detail")
    def _validate_detail(self, key: str, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """A second, independent layer: scrub on every ORM attribute
        assignment, not just inside ``audit_service.record``.

        This closes the gap where something constructs ``AuditLog(...)``
        directly instead of calling ``record`` -- accidentally or as a
        shortcut -- and would otherwise skip ``record``'s own call to
        ``scrub_detail``. It does **not** close every gap: SQLAlchemy Core
        inserts and the ``Session`` bulk helpers write columns without ever
        assigning a Python attribute on an ``AuditLog`` instance, so
        ``@validates`` never fires for them either. See ``scrub_detail``'s
        docstring above for the full, accurate list of what is and is not
        covered -- this hook is one of two layers, not the whole guarantee.
        """
        return scrub_detail(value)
