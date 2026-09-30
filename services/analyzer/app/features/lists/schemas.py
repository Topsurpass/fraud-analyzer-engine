"""Request and response models for named lists.

The item limit is a setting (``FAE_MAX_LIST_ITEMS``), so it is enforced in the
service; the per-item length is fixed and enforced here.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.types import UtcDatetime

#: Matches ``ListItem.value``'s column width.
MAX_ITEM_LENGTH = 500

def _no_nul(value: str) -> str:
    """Postgres refuses NUL in text with a driver error (a 500); SQLite stores it.
    Refuse it up front so both behave the same."""
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


Item = Annotated[
    str, StringConstraints(max_length=MAX_ITEM_LENGTH), AfterValidator(_no_nul)
]


class ItemListWrite(BaseModel):
    """Body of POST and PUT. PUT replaces name, description and items whole."""

    name: Annotated[str, AfterValidator(_no_nul)] = Field(min_length=1, max_length=200)
    description: Annotated[str, AfterValidator(_no_nul)] | None = Field(
        default=None, max_length=1000
    )
    items: list[Item] = Field(default_factory=list)


class ItemListSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    item_count: int
    #: Distinct flag rules that use the list.
    rule_count: int
    created_by: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ItemListRead(ItemListSummary):
    items: list[str]


class ItemListWriteResult(ItemListRead):
    """What a save did with the items it was sent."""

    received: int
    kept: int
    #: ``received - kept``: repeats (by match key, so ``A1`` and ``a1 `` count)
    #: plus blank entries.
    duplicates_dropped: int
