"""The stored flagged-row queue.

Storing findings changes what the flagged view is. It used to be recomputed
from the result cache, so it existed only while a cache entry did and only if
somebody had recently opened the query; a restart emptied it. Now it is a queue
that accumulates while nobody is watching.

The contract is "the stored set equals what the rules currently match", and
every test here is one consequence of that sentence.
"""

from __future__ import annotations

import pytest

from tests.test_flag_rules_api import make_query, rule


@pytest.fixture
def flagging_query(client, sqlite_connection):
    """A query whose rule matches two of the five seeded rows (900.0, 750.25)."""
    created = make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, amount, comment FROM txns",
        name="stored",
    )
    client.put(
        f"/queries/{created['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    return created


def _section(client, connection_id, name="stored"):
    body = client.get(f"/connections/{connection_id}/flagged").json()
    return next(s for s in body["queries"] if s["query_name"] == name)


def test_running_the_query_stores_what_it_matched(
    client, sqlite_connection, flagging_query
):
    client.post(f"/queries/{flagging_query['id']}/run")
    section = _section(client, sqlite_connection["id"])
    assert section["flagged_count"] == 2
    assert section["stale"] is False


def test_the_queue_survives_the_result_cache_expiring(
    client, sqlite_connection, flagging_query
):
    """The reason for storing them at all.

    The old view read the cache, so once the entry aged out it reported "not
    run yet" about a query that had been flagging rows for days.
    """
    from app.services import result_cache

    client.post(f"/queries/{flagging_query['id']}/run")
    result_cache.invalidate(flagging_query["id"])

    section = _section(client, sqlite_connection["id"])
    assert section["flagged_count"] == 2


def test_a_finding_keeps_the_time_it_was_first_seen(
    client, sqlite_connection, flagging_query
):
    # "This has been sitting here for three days" is the fact an analyst acts
    # on, and a re-run every poll interval must not reset it.
    client.post(f"/queries/{flagging_query['id']}/run")
    first = _section(client, sqlite_connection["id"])["rows"][0]["first_seen_at"]

    client.post(f"/queries/{flagging_query['id']}/run")
    again = _section(client, sqlite_connection["id"])["rows"][0]["first_seen_at"]
    assert again == first


def test_re_running_does_not_duplicate_a_finding(
    client, sqlite_connection, flagging_query
):
    for _ in range(3):
        client.post(f"/queries/{flagging_query['id']}/run")
    assert _section(client, sqlite_connection["id"])["flagged_count"] == 2


def test_a_row_that_stops_matching_is_removed(
    client, sqlite_connection, flagging_query
):
    # The table is the present, not the history: raise the threshold above one
    # of the two matches and it should leave the queue.
    client.post(f"/queries/{flagging_query['id']}/run")
    assert _section(client, sqlite_connection["id"])["flagged_count"] == 2

    client.put(
        f"/queries/{flagging_query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "800")]},
    )
    client.post(f"/queries/{flagging_query['id']}/run")
    assert _section(client, sqlite_connection["id"])["flagged_count"] == 1


def test_removing_every_rule_empties_the_queue(
    client, sqlite_connection, flagging_query
):
    client.post(f"/queries/{flagging_query['id']}/run")
    client.put(f"/queries/{flagging_query['id']}/flag-rules", json={"rules": []})
    client.post(f"/queries/{flagging_query['id']}/run")

    body = client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    # No rules means no section at all, not a section holding stale findings.
    assert body["queries"] == []


def test_a_finding_carries_its_own_column_headers(
    client, sqlite_connection, flagging_query
):
    # Stored with the row so it still renders correctly after the SELECT list
    # changes, instead of silently relabelling every earlier finding.
    client.post(f"/queries/{flagging_query['id']}/run")
    section = _section(client, sqlite_connection["id"])
    assert section["columns"] == ["day", "amount", "comment"]


def test_dismissing_deletes_the_stored_row(client, sqlite_connection, flagging_query):
    client.post(f"/queries/{flagging_query['id']}/run")
    section = _section(client, sqlite_connection["id"])
    victim = section["rows"][0]["fingerprint"]

    client.post(
        f"/queries/{flagging_query['id']}/flag-dismissals",
        json={"fingerprints": [victim]},
    )

    from app.db.app_state import get_engine
    from app.models import FlaggedRow
    from sqlalchemy.orm import Session

    with Session(get_engine()) as session:
        stored = [r.row_fingerprint for r in session.query(FlaggedRow).all()]
    assert victim not in stored


def test_a_dismissed_row_is_not_stored_again_by_the_next_run(
    client, sqlite_connection, flagging_query
):
    """Without this the queue refills itself and dismissing means nothing."""
    client.post(f"/queries/{flagging_query['id']}/run")
    victim = _section(client, sqlite_connection["id"])["rows"][0]["fingerprint"]
    client.post(
        f"/queries/{flagging_query['id']}/flag-dismissals",
        json={"fingerprints": [victim]},
    )

    client.post(f"/queries/{flagging_query['id']}/run")
    section = _section(client, sqlite_connection["id"])
    assert section["flagged_count"] == 1
    assert victim not in [row["fingerprint"] for row in section["rows"]]


def test_deleting_findings_does_not_suppress_them(
    client, sqlite_connection, flagging_query
):
    """Delete and dismiss are deliberately different.

    Dismissing is a decision and is remembered. Deleting only clears what is
    stored now - for tidying a queue after a rule change - so a row that still
    matches comes back on the next run.
    """
    client.post(f"/queries/{flagging_query['id']}/run")
    removed = client.delete(f"/queries/{flagging_query['id']}/flagged-rows")
    assert removed.json()["changed"] == 2
    assert _section(client, sqlite_connection["id"])["flagged_count"] == 0

    client.post(f"/queries/{flagging_query['id']}/run")
    assert _section(client, sqlite_connection["id"])["flagged_count"] == 2


def test_deleting_the_query_takes_its_findings(client, flagging_query):
    client.post(f"/queries/{flagging_query['id']}/run")
    client.delete(f"/queries/{flagging_query['id']}")

    from app.db.app_state import get_engine
    from app.models import FlaggedRow
    from sqlalchemy.orm import Session

    with Session(get_engine()) as session:
        assert session.query(FlaggedRow).count() == 0


def test_the_summary_counts_by_connection_and_query(
    client, sqlite_connection, flagging_query
):
    client.post(f"/queries/{flagging_query['id']}/run")
    summary = client.get("/flagged/summary").json()

    assert summary["flagged_count"] == 2
    connection = next(
        c for c in summary["connections"] if c["connection_id"] == sqlite_connection["id"]
    )
    assert connection["flagged_count"] == 2
    assert connection["severity"] == "high"
    # The name travels with the count: a notification listing a uuid is not a
    # notification.
    assert connection["connection_name"] == sqlite_connection["name"]

    query_entry = next(
        q for q in summary["queries"] if q["query_id"] == flagging_query["id"]
    )
    assert query_entry["flagged_count"] == 2


def test_the_summary_is_empty_with_nothing_flagged(client):
    summary = client.get("/flagged/summary").json()
    assert summary == {
        "connections": [],
        "queries": [],
        "flagged_count": 0,
        "newest_first_seen_at": None,
    }


def test_a_rule_matching_nothing_still_appears_in_the_legend(
    client, sqlite_connection, flagging_query
):
    # "Large transfer 0" is the answer to "did my rule stop working".
    client.put(
        f"/queries/{flagging_query['id']}/flag-rules",
        json={"rules": [rule("Impossible", "amount", "gt", "999999")]},
    )
    client.post(f"/queries/{flagging_query['id']}/run")
    section = _section(client, sqlite_connection["id"])
    assert [(r["name"], r["matched"]) for r in section["rules"]] == [("Impossible", 0)]


# ---------------------------------------------------------------------------
# What the notification bell reads
#
# A count alone cannot answer "has anything new arrived": dismiss two, gain
# two, and the count has not moved while the reader has still missed something.
# The newest first_seen_at is what makes "new since you last looked" mean
# anything.
# ---------------------------------------------------------------------------


def test_the_summary_says_when_the_newest_finding_appeared(
    client, sqlite_connection, flagging_query
):
    client.post(f"/queries/{flagging_query['id']}/run")
    summary = client.get("/flagged/summary").json()

    assert summary["newest_first_seen_at"] is not None
    section = _section(client, sqlite_connection["id"])
    assert summary["newest_first_seen_at"] == max(
        row["first_seen_at"] for row in section["rows"]
    )


def test_each_connection_carries_its_own_newest(
    client, sqlite_connection, flagging_query
):
    client.post(f"/queries/{flagging_query['id']}/run")
    summary = client.get("/flagged/summary").json()
    entry = next(
        c for c in summary["connections"] if c["connection_id"] == sqlite_connection["id"]
    )
    assert entry["newest_first_seen_at"] == summary["newest_first_seen_at"]


def test_the_newest_is_null_when_nothing_is_flagged(client):
    assert client.get("/flagged/summary").json()["newest_first_seen_at"] is None


def test_re_running_does_not_move_the_newest(client, flagging_query):
    """first_seen_at is preserved on a re-run, so the bell must not re-alert.

    Otherwise every scheduled run would look like new findings had arrived and
    the notification would be permanently lit.
    """
    client.post(f"/queries/{flagging_query['id']}/run")
    first = client.get("/flagged/summary").json()["newest_first_seen_at"]
    client.post(f"/queries/{flagging_query['id']}/run")
    assert client.get("/flagged/summary").json()["newest_first_seen_at"] == first
