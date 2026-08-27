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

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, UTCDateTime, new_id, utcnow
from app.models.enums import AuditAction, enum_column


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

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )
