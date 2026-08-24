"""The fingerprint that gives a recomputed row an identity.

Flagged rows are never stored, so "this row was reviewed" has to be recorded
against a hash of the row's contents. Every property below is load-bearing for
whether that is safe to use in a fraud queue.
"""

from __future__ import annotations

from app.services.flag_dismissal_service import row_fingerprint


def test_the_same_row_always_hashes_the_same():
    row = ["2026-08-21", 900.0, "big"]
    assert row_fingerprint(row) == row_fingerprint(list(row))


def test_a_changed_value_is_a_different_row():
    # The property the whole design rests on: correct an amount and the row is
    # no longer the one that was cleared, so it returns to the queue.
    assert row_fingerprint(["a", 900.0]) != row_fingerprint(["a", 901.0])


def test_column_order_matters():
    # Two different rows can hold the same values in a different order.
    assert row_fingerprint(["a", "b"]) != row_fingerprint(["b", "a"])


def test_null_is_distinct_from_the_empty_string():
    # SQL's own distinction, and one a fraud reviewer would care about.
    assert row_fingerprint([None]) != row_fingerprint([""])


def test_a_number_is_distinct_from_its_text():
    # Postgres returns Decimal as a string and SQLite returns a float for the
    # same column, so this is the difference between two drivers, not two rows.
    # Nothing here bridges them: a dismissal is per query, and one query does
    # not change driver between runs.
    assert row_fingerprint([900]) != row_fingerprint(["900"])


def test_false_is_distinct_from_zero():
    assert row_fingerprint([False]) != row_fingerprint([0])


def test_the_hash_is_a_sha256_hex_digest():
    # The schema validator rejects anything else, so the producer and the
    # validator have to agree on the shape.
    digest = row_fingerprint(["anything"])
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_an_empty_row_still_hashes():
    assert len(row_fingerprint([])) == 64


def test_nested_values_are_handled():
    # to_jsonable can emit a list or dict for a JSON column.
    assert row_fingerprint([{"a": 1}]) != row_fingerprint([{"a": 2}])


def test_key_order_inside_a_nested_object_does_not_matter():
    # Same object, two serialisation orders, one row.
    assert row_fingerprint([{"a": 1, "b": 2}]) == row_fingerprint([{"b": 2, "a": 1}])
