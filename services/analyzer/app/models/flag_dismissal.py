"""Flagged rows an analyst has reviewed and does not want to see again.

Flagged rows are not stored. They are recomputed from a query's cached result
every time the flagged view is opened, which is what keeps the engine from
holding a second copy of a customer's data. That leaves nothing to attach a
"dismissed" marker to, so a dismissal records a *fingerprint* of the row's
contents instead: a hash over the values as they were serialised for the wire.

The consequence is deliberate and worth stating, because it decides whether
this is useful for review or actively dangerous. If the underlying row changes
in any way -- a status moves, an amount is corrected -- its fingerprint changes
with it, so it is no longer the row that was dismissed and it comes back. A
dismissal says "I looked at this exact row and it is fine", never "stop telling
me about this account".

Only the hash is stored, never the values, so this table cannot be read back
into the data it describes.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, new_id


class FlagDismissal(TimestampMixin, Base):
    __tablename__ = "flag_dismissals"
    __table_args__ = (
        # Dismissing the same row twice is a no-op, not an error, and the index
        # is also what makes the per-query lookup on every flagged-view load a
        # single seek rather than a scan.
        UniqueConstraint("query_id", "row_fingerprint", name="uq_flag_dismissals_row"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    query_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: sha256 hex of the row's values. Scoped to the query: the same values in
    #: two different queries are two different findings to review.
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
