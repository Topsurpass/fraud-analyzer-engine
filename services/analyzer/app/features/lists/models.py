"""Named lists of values, shared by every signed-in user.

A watchlist of 400 account numbers used to be pasted into every rule that
needed it. Here it is stored once, named, and referenced by ``list_id`` from a
flag condition using ``in_list`` / ``not_in_list``.

Items are rows rather than a JSON column so the database enforces the
ownership (a list's items cannot outlive it) and so a list of 50,000 values can
be replaced with one bulk insert instead of rewriting a single huge cell.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_id

class ListItem(Base):
    __tablename__ = "list_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    list_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("item_lists.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: As the user typed it, trimmed. Matching goes through ``list_key`` in
    #: ``matching.py``, never through this text directly.
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Order the user gave them in, so the editor shows the list as it was pasted.
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ItemList(TimestampMixin, Base):
    __tablename__ = "item_lists"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: ``matching.name_key(name)``: NFC and case-folded. Unique across every
    #: list, because lists are shared and two people each keeping "Blocked
    #: terminals" and "blocked terminals" would make the rule editor's picker
    #: ambiguous. Computed in Python and stored, rather than a ``lower(name)``
    #: index, so SQLite and Postgres agree on non-ASCII names. 600 wide because
    #: case folding can triple a character ("ß" becomes "ss", some go to three).
    name_key: Mapped[str] = mapped_column(String(600), nullable=False, unique=True, index=True)
    #: Incremented on every save. The in-process member-set cache keys on it, so
    #: an edit can never be masked by a stale entry (a timestamp could repeat).
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Who made it, and so who besides an admin may change it. Nullable and
    #: SET NULL so a list survives its creator; an unowned list is then
    #: editable by admins only.
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Not loaded with the list. A list can hold 50,000 items and most reads
    #: (the picker, the summary) need none of them.
    items: Mapped[list[ListItem]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=ListItem.position,
    )

