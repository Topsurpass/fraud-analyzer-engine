"""Named list lifecycle.

Lists are shared: every signed-in user may read and use any list. Changing or
deleting one is for its creator or an administrator.

Editing items must reach flags that are already on screen. A cached poll result
holds flags computed from the old items, so saving invalidates the result cache
of every query whose rules read the list; the next poll re-evaluates.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.config import get_settings
from app.db.base import new_id, utcnow
from app.errors import AppError, ErrorCode, NotFoundError
from app.features.flag_rules.models import FlagCondition, FlagRule
from app.features.lists.matching import _tokens, cached_members, list_key, name_key
from app.features.lists.models import ItemList, ListItem
from app.features.lists.schemas import ItemListWrite
from app.features.queries import result_cache
from app.features.queries import service as saved_query_service
from app.features.queries.models import SavedQuery
from app.features.users.models import User


@dataclass(slots=True)
class ListStats:
    item_count: int
    rule_count: int


@dataclass(slots=True)
class SaveReport:
    received: int
    kept: int

    @property
    def duplicates_dropped(self) -> int:
        return self.received - self.kept


def get_list(session: Session, list_id: str) -> ItemList:
    item_list = session.get(ItemList, list_id)
    if item_list is None:
        raise NotFoundError(
            ErrorCode.LIST_NOT_FOUND,
            f"No list with id {list_id!r}.",
            {"list_id": list_id},
        )
    return item_list


def can_change(item_list: ItemList, user: User) -> bool:
    return user.is_admin or (
        item_list.created_by is not None and item_list.created_by == user.id
    )


def get_changeable(session: Session, list_id: str, user: User) -> ItemList:
    """A list the caller may edit or delete.

    403 rather than the 404 ``get_owned`` uses elsewhere: the list is readable by
    everyone, so there is no existence to hide, and a clear refusal tells the
    person why the Save button failed.
    """
    item_list = get_list(session, list_id)
    if not can_change(item_list, user):
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Only the person who created this list, or an admin, can change it.",
            {"list_id": list_id},
        )
    return item_list


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def stats_for(session: Session, list_ids: list[str] | None = None) -> dict[str, ListStats]:
    """Item and rule counts, two grouped statements however many lists there are."""
    items = select(ListItem.list_id, func.count()).group_by(ListItem.list_id)
    rules = (
        select(FlagCondition.list_id, func.count(sa.distinct(FlagCondition.rule_id)))
        .where(FlagCondition.list_id.is_not(None))
        .group_by(FlagCondition.list_id)
    )
    if list_ids is not None:
        items = items.where(ListItem.list_id.in_(list_ids))
        rules = rules.where(FlagCondition.list_id.in_(list_ids))
    item_counts = {key: n for key, n in session.execute(items).all()}
    rule_counts = {key: n for key, n in session.execute(rules).all()}
    keys = set(item_counts) | set(rule_counts) | set(list_ids or [])
    return {
        key: ListStats(item_counts.get(key, 0), rule_counts.get(key, 0)) for key in keys
    }


def list_lists(session: Session) -> list[tuple[ItemList, ListStats]]:
    """Every list, by name, with counts. Never loads the items themselves."""
    lists = list(session.scalars(select(ItemList).order_by(func.lower(ItemList.name))))
    stats = stats_for(session)
    return [(item_list, stats.get(item_list.id, ListStats(0, 0))) for item_list in lists]


def stats_of(session: Session, item_list: ItemList) -> ListStats:
    return stats_for(session, [item_list.id]).get(item_list.id, ListStats(0, 0))


def item_values(session: Session, item_list: ItemList) -> list[str]:
    return list(
        session.scalars(
            select(ListItem.value)
            .where(ListItem.list_id == item_list.id)
            .order_by(ListItem.position)
        )
    )


def rules_using(session: Session, list_id: str, user: User) -> tuple[list[dict], int]:
    """Rules that read the list: those on queries the caller can see, and how many more.

    Queries are private to their owner, so naming another analyst's query or
    rule in an error would leak it. The hidden ones are only counted.
    """
    rows = session.execute(
        select(FlagRule.id, FlagRule.name, SavedQuery.id, SavedQuery.name)
        .join(FlagCondition, FlagCondition.rule_id == FlagRule.id)
        .join(SavedQuery, SavedQuery.id == FlagRule.query_id)
        .where(FlagCondition.list_id == list_id)
        .distinct()
    ).all()
    query_ids = {row[2] for row in rows}
    seen = set(
        session.scalars(
            select(SavedQuery.id).where(
                SavedQuery.id.in_(query_ids), saved_query_service.visible_to(user)
            )
        )
    ) if query_ids else set()
    # Sorted here, not in SQL, so the order does not depend on the database's
    # collation (SQLite and Postgres disagree about case).
    visible = sorted(
        (row for row in rows if row[2] in seen),
        key=lambda r: (r[3].lower(), r[1].lower(), r[2]),
    )
    return (
        [
            {"rule_name": rule_name, "query_id": query_id, "query_name": query_name}
            for _, rule_name, query_id, query_name in visible
        ],
        len(rows) - len(visible),
    )


def is_used(session: Session, list_id: str) -> bool:
    return (
        session.scalar(
            select(FlagCondition.id).where(FlagCondition.list_id == list_id).limit(1)
        )
        is not None
    )


def members_of(item_list: ItemList, session: Session | None = None) -> frozenset:
    """Match keys for one list, cached per (list, version).

    Items load with a column select, never as ORM objects: a 50,000-item list
    would otherwise build 50,000 of them on every cache miss.
    """
    session = session or object_session(item_list)
    list_id = item_list.id
    return cached_members(
        list_id, item_list.version, lambda: item_values(session, item_list)
    )


def members_for(session: Session, list_ids: set[str]) -> dict[str, frozenset[str]]:
    """Member keys per list id, for callers that build specs from unsaved rules.

    Raises LIST_NOT_FOUND naming any id that is not a stored list.
    """
    if not list_ids:
        return {}
    found = {
        item_list.id: item_list
        for item_list in session.scalars(
            select(ItemList).where(ItemList.id.in_(list_ids))
        )
    }
    missing = sorted(list_ids - set(found))
    if missing:
        raise NotFoundError(
            ErrorCode.LIST_NOT_FOUND,
            f"No list with id {missing[0]!r}.",
            {"list_ids": missing},
        )
    return {list_id: members_of(item_list, session) for list_id, item_list in found.items()}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _clean_items(items: list[str]) -> tuple[list[str], int]:
    """Trim, drop blanks, and de-duplicate by match key keeping first spelling.

    Returns the kept values and how many were received. De-duplicating on the
    key (not the text) is what keeps ``2`` and ``2.0`` from both being stored
    when they would match the same cells.
    """
    limit = get_settings().max_list_items
    if len(items) > limit:
        raise AppError(
            ErrorCode.REQUEST_VALIDATION_ERROR,
            f"A list can hold at most {limit} items; this one has {len(items)}.",
            {"max_items": limit, "received": len(items)},
        )
    # De-duplicate on the full match signature, not the key alone: "1" also
    # matches a boolean true cell and "1.0" does not, so keeping whichever came
    # first would make the list's behaviour depend on paste order.
    seen: set[frozenset] = set()
    kept: list[str] = []
    for raw in items:
        value = raw.strip()
        if not value:
            continue
        signature = frozenset({list_key(value), *_tokens(value)})
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(value)
    return kept, len(items)


def _name_taken(session: Session, name: str, except_id: str | None) -> bool:
    statement = select(ItemList.id).where(ItemList.name_key == name_key(name))
    if except_id is not None:
        statement = statement.where(ItemList.id != except_id)
    return session.scalar(statement) is not None


def _raise_name_taken(name: str):
    raise AppError(
        ErrorCode.LIST_NAME_TAKEN,
        f"A list named {name!r} already exists.",
        {"name": name},
    )


def _replace_items(session: Session, item_list: ItemList, values: list[str]) -> None:
    """Swap the whole item set with one delete and one bulk insert.

    Going through the relationship would build 50,000 ORM objects; this is a
    single executemany.
    """
    session.execute(delete(ListItem).where(ListItem.list_id == item_list.id))
    if values:
        session.execute(
            insert(ListItem),
            [
                {"id": new_id(), "list_id": item_list.id, "value": value, "position": i}
                for i, value in enumerate(values)
            ],
        )
    session.expire(item_list, ["items"])


def create_list(session: Session, payload: ItemListWrite, user: User) -> tuple[ItemList, SaveReport]:
    name = payload.name.strip()
    if not name:
        raise AppError(ErrorCode.REQUEST_VALIDATION_ERROR, "A list needs a name.")
    values, received = _clean_items(payload.items)
    if _name_taken(session, name, None):
        _raise_name_taken(name)

    item_list = ItemList(
        name=name,
        name_key=name_key(name),
        description=(payload.description or "").strip() or None,
        created_by=user.id,
    )
    session.add(item_list)
    try:
        session.flush()
        _replace_items(session, item_list, values)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise AppError(
            ErrorCode.LIST_NAME_TAKEN,
            f"A list named {name!r} already exists.",
            {"name": name},
        ) from exc
    session.refresh(item_list)
    return item_list, SaveReport(received=received, kept=len(values))


def _query_ids_using(session: Session, list_id: str) -> list[str]:
    return list(
        session.scalars(
            select(FlagRule.query_id)
            .join(FlagCondition, FlagCondition.rule_id == FlagRule.id)
            .where(FlagCondition.list_id == list_id)
            .distinct()
        )
    )


def update_list(
    session: Session, item_list: ItemList, payload: ItemListWrite
) -> SaveReport:
    """Replace name, description and items, then drop cached flags that used them."""
    name = payload.name.strip()
    if not name:
        raise AppError(ErrorCode.REQUEST_VALIDATION_ERROR, "A list needs a name.")
    values, received = _clean_items(payload.items)
    if _name_taken(session, name, item_list.id):
        _raise_name_taken(name)

    item_list.name = name
    item_list.name_key = name_key(name)
    item_list.description = (payload.description or "").strip() or None
    # Bumped by hand: replacing items touches no column of the list row, so the
    # ORM's onupdate would not fire. `version` is what the member-set cache keys
    # on; as a SQL expression so two racing saves cannot both write the same n.
    item_list.updated_at = utcnow()
    item_list.version = ItemList.version + 1
    try:
        _replace_items(session, item_list, values)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise AppError(
            ErrorCode.LIST_NAME_TAKEN,
            f"A list named {name!r} already exists.",
            {"name": name},
        ) from exc

    # Cached results carry flags computed from the old items; without this a
    # poll would keep serving them and report that nothing changed.
    for query_id in _query_ids_using(session, item_list.id):
        result_cache.invalidate(query_id)

    session.refresh(item_list)
    return SaveReport(received=received, kept=len(values))


def delete_list(session: Session, item_list: ItemList, user: User) -> None:
    """Delete an unused list; refuse with LIST_IN_USE naming the rules otherwise.

    Only rules on queries the caller can see are named (``detail.rules``); the
    rest are counted in ``detail.hidden_rule_count``.
    """
    list_id = item_list.id
    list_name = item_list.name

    def in_use() -> AppError:
        rules, hidden = rules_using(session, list_id, user)
        names = ", ".join(sorted({rule["rule_name"] for rule in rules}))
        parts = [f"List {list_name!r} is used by"]
        if rules:
            parts.append(f": {names}")
        if hidden:
            parts.append(
                (" and " if rules else ": ")
                + f"{hidden} rule{'s' if hidden != 1 else ''} on queries you cannot see"
            )
        return AppError(
            ErrorCode.LIST_IN_USE,
            "".join(parts) + ". Remove it from those rules first.",
            {"list_id": list_id, "rules": rules, "hidden_rule_count": hidden},
        )

    if is_used(session, list_id):
        raise in_use()
    try:
        session.delete(item_list)
        session.commit()
    except IntegrityError as exc:
        # A rule started using the list after the check. The RESTRICT foreign
        # key is what stops the delete; report it the same way.
        session.rollback()
        raise in_use() from exc
