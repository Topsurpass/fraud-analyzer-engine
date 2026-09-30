"""How a list decides a cell is a member. Pure: no database, no session.

A list is a set of *keys*, not of raw strings. The key of an item and the key of
a result cell come from the same function, so "is this cell in the list" is one
set lookup however many items the list holds.

The rules follow what ``in`` does with a typed value list, so moving a pasted
list of ids into a named list does not change which rows flag, with one
deliberate difference:

* numbers compare as numbers, at full precision, so ``2.0`` in the list catches
  a cell of ``2`` and ``"2.00"``, and 31-digit ids stay distinct;
* booleans bridge the way ``in`` does: a real ``bool`` cell matches an item
  spelled true/t/yes/y/1 (or false/f/no/n/0), and a text cell that is a bool
  word matches an item that is a bool word;
* everything else compares as text, trimmed and case-folded. ``in`` is
  case-sensitive; a list is not, on purpose, because nobody wants ``ACME-1`` and
  ``acme-1`` to be different accounts. Unicode is not normalised, so a composed
  and a decomposed ``e`` with an accent are different values.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable


def as_number(value: Any) -> Decimal | None:
    """Return ``value`` as a Decimal, or None if it is not a number.

    ``Decimal`` rather than ``float`` because the values that arrive here are
    frequently *strings that used to be Decimals* -- ``to_jsonable`` converts
    them precisely so that money does not round-trip through binary floating
    point. Parsing them back as floats would reintroduce the error the string
    conversion existed to avoid: ``0.1 + 0.2 > 0.3`` is True in float and False
    in Decimal, and a threshold rule sitting exactly on a boundary would flag
    inconsistently.

    ``bool`` is excluded on purpose even though it is an ``int`` subclass.
    ``True > 0`` is technically valid Python and completely meaningless as a
    flag condition; treating booleans as text keeps ``eq true`` working the way
    an analyst expects.

    Lives here rather than in the flag-rule engine because the engine needs
    list matching and list matching needs this; one direction of import, no
    cycle. The engine re-exports it under the same name.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return None if value.is_nan() else value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # NaN and infinities cannot participate in an ordering that means
        # anything. to_jsonable already nulls them out of result rows, but a
        # rule's own value string could still spell one.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        # "nan" and "infinity" parse as Decimal but are not comparable.
        return None if not parsed.is_finite() else parsed
    return None


#: Words a two-valued column spells itself with, excluding the digits 0 and 1.
#: Kept word-only on purpose. Bridging digits to words would make the rule
#: ``country eq 0`` match a cell of ``"NO"``, which is Norway's ISO code, not a
#: false. Digits reach booleans only through a genuine ``bool`` cell.
_TRUE_WORDS = frozenset({"true", "t", "yes", "y"})
_FALSE_WORDS = frozenset({"false", "f", "no", "n"})
_BOOL_WORDS = _TRUE_WORDS | _FALSE_WORDS


def as_bool(value: Any) -> bool | None:
    """Interpret a driver's idea of a boolean, or None if it is not one.

    Drivers disagree: Postgres hands back ``True``, SQLite hands back ``1``,
    and a text column holding a flag might say ``"t"`` or ``"yes"``. An analyst
    should not have to know which of those their database chose.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS or text == "1":
            return True
        if text in _FALSE_WORDS or text == "0":
            return False
    return None


def is_bool_word(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _BOOL_WORDS


def list_key(value: Any) -> str:
    """The comparison key of one item or one cell.

    A number becomes its exact decimal digits with trailing zeros stripped, so
    ``2``, ``2.0`` and ``"2.00"`` share a key and ``0``, ``-0`` and ``0.000``
    share another. Built from ``as_tuple()`` rather than ``normalize()``: the
    latter rounds to the context precision (28 digits) and would make
    ``1234567890123456789012345678901`` and ``...902`` the same key. There is no
    expansion either, so ``1e999999999`` stays a short key.

    Anything else is the trimmed, case-folded text. ``casefold`` rather than
    ``lower`` so ``"STRASSE"`` and ``"straße"`` agree.
    """
    number = as_number(value)
    if number is not None:
        sign, digits, exponent = number.as_tuple()
        text = "".join(map(str, digits)).rstrip("0")
        if not text:
            return "0"
        exponent += len(digits) - len(text)
        return f"{'-' if sign else ''}{text}" + (f"e{exponent}" if exponent else "")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().casefold()


def name_key(name: str) -> str:
    """Uniqueness key for a list name: NFC, case-folded, trimmed.

    Computed in Python and stored, so SQLite (ASCII-only ``lower()``) and
    Postgres agree that ``Éclair`` and ``éclair`` are the same name.
    """
    folded = unicodedata.normalize("NFC", name.strip()).casefold()
    return unicodedata.normalize("NFC", folded)


def _tokens(item: str) -> list:
    """Extra set members that let a list item match boolean cells.

    Tuples, never strings, so no cell's text can collide with them.
    """
    extra: list = []
    as_flag = as_bool(item)
    if as_flag is not None:
        extra.append(("bool", as_flag))
    if is_bool_word(item):
        extra.append(("word", as_flag))
    return extra


def members_from_items(items: Iterable[str]) -> frozenset:
    """The set of keys for a list's items. Blank items are not members.

    A blank item would otherwise make every empty-string cell a member, which
    is never what pasting a trailing newline meant.
    """
    keys: set = set()
    for item in items:
        key = list_key(item)
        if not key:
            continue
        keys.add(key)
        keys.update(_tokens(item))
    return frozenset(keys)


def is_member(members: frozenset, cell: Any) -> bool:
    """Whether a (non-null) cell is in a list's member set."""
    if list_key(cell) in members:
        return True
    if isinstance(cell, bool):
        # A real bool matches any item that reads as that boolean, as `in` does.
        return ("bool", cell) in members
    if is_bool_word(cell):
        return ("word", as_bool(cell)) in members
    return False


# ---------------------------------------------------------------------------
# Member-set cache
# ---------------------------------------------------------------------------

#: Building the set for a 50,000-item list costs tens of milliseconds and every
#: poll of every query using it would pay that again. Keyed by list id *and* its
#: integer ``version``, which every edit increments, so an edit changes the key
#: and a stale set is never served. Not a timestamp: two edits can share one
#: under a coarse or stepped clock. Bounded so a long-running
#: process that has seen many lists does not keep them all.
_MAX_CACHED_LISTS = 32
_cache: "OrderedDict[tuple[str, Any], frozenset]" = OrderedDict()
_lock = threading.Lock()


def cached_members(
    list_id: str, version: Any, load_items: Callable[[], Iterable[str]]
) -> frozenset:
    """``members_from_items(load_items())``, remembered per (list, version).

    ``load_items`` is called only on a miss, so a hit never touches the database.
    """
    key = (list_id, version)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    members = members_from_items(load_items())
    with _lock:
        _cache[key] = members
        _cache.move_to_end(key)
        while len(_cache) > _MAX_CACHED_LISTS:
            _cache.popitem(last=False)
    return members


def clear_cache() -> None:
    with _lock:
        _cache.clear()
