"""Flag-rule storage, flagged runs, and the per-connection flagged view.

The target fixture (``tests/conftest.py``) is deliberately a schema with no
flag column of any kind: `txns` has day, user_id, amount, flagged, comment. The
tests here mostly select columns that carry no hint of fraud, because the whole
point of the feature is that flagging must work on a schema the engine knows
nothing about.
"""

from __future__ import annotations

import pytest


def make_query(admin_client, connection_id, sql, name="q", **extra):
    response = admin_client.post(
        f"/connections/{connection_id}/queries",
        json={"name": name, "sql_text": sql, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def rule(name, column, operator, value=None, value2=None, severity="high",
         enabled=True):
    condition = {"column_name": column, "operator": operator}
    if value is not None:
        condition["value"] = value
    if value2 is not None:
        condition["value2"] = value2
    return {
        "name": name,
        "severity": severity,
        "enabled": enabled,
        "conditions": [condition],
    }


@pytest.fixture
def query(admin_client, sqlite_connection):
    return make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, amount, comment FROM txns ORDER BY id",
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_a_new_query_has_no_rules(admin_client, query):
    response = admin_client.get(f"/queries/{query['id']}/flag-rules")
    assert response.status_code == 200
    assert response.json() == {"query_id": query["id"], "rules": []}


def test_rules_round_trip(admin_client, query):
    payload = {"rules": [rule("Large", "amount", "gt", "500")]}
    response = admin_client.put(f"/queries/{query['id']}/flag-rules", json=payload)
    assert response.status_code == 200, response.text

    stored = response.json()["rules"]
    assert len(stored) == 1
    assert stored[0]["name"] == "Large"
    assert stored[0]["severity"] == "high"
    assert stored[0]["position"] == 0
    assert stored[0]["conditions"][0]["column_name"] == "amount"
    assert stored[0]["conditions"][0]["operator"] == "gt"

    again = admin_client.get(f"/queries/{query['id']}/flag-rules").json()
    assert again["rules"] == stored


def test_put_replaces_rather_than_appends(admin_client, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("First", "amount", "gt", "1")]},
    )
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Second", "amount", "gt", "2")]},
    )
    names = [r["name"] for r in response.json()["rules"]]
    assert names == ["Second"]


def test_an_empty_list_clears_every_rule(admin_client, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    response = admin_client.put(f"/queries/{query['id']}/flag-rules", json={"rules": []})
    assert response.json()["rules"] == []


def test_position_follows_list_order(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={
            "rules": [
                rule("C", "amount", "gt", "3"),
                rule("A", "amount", "gt", "1"),
                rule("B", "amount", "gt", "2"),
            ]
        },
    )
    stored = response.json()["rules"]
    assert [r["name"] for r in stored] == ["C", "A", "B"]
    assert [r["position"] for r in stored] == [0, 1, 2]


def test_duplicate_rule_names_are_refused(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={
            "rules": [
                rule("Same", "amount", "gt", "1"),
                rule("same", "amount", "gt", "2"),
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


def test_rules_on_an_unknown_query_are_a_404(admin_client):
    response = admin_client.get("/queries/does-not-exist/flag-rules")
    assert response.status_code == 404
    assert response.json()["error_code"] == "QUERY_NOT_FOUND"


def test_deleting_a_query_takes_its_rules_with_it(admin_client, query, session):
    from app.models import FlagCondition, FlagRule

    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    assert session.query(FlagRule).count() == 1
    assert session.query(FlagCondition).count() == 1

    assert admin_client.delete(f"/queries/{query['id']}").status_code == 204

    session.expire_all()
    assert session.query(FlagRule).count() == 0
    # The condition must go too, or the next query to reuse the id inherits it.
    assert session.query(FlagCondition).count() == 0


# ---------------------------------------------------------------------------
# Validation of rule shape
# ---------------------------------------------------------------------------


def test_a_rule_needs_at_least_one_condition(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [{"name": "Empty", "severity": "low", "conditions": []}]},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("operator", ["gt", "eq", "contains", "in", "starts_with"])
def test_operators_that_read_a_value_must_be_given_one(admin_client, query, operator):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("No value", "amount", operator)]},
    )
    assert response.status_code == 422


def test_between_needs_both_bounds(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Half range", "amount", "between", "100")]},
    )
    assert response.status_code == 422

    ok = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Range", "amount", "between", "100", "900")]},
    )
    assert ok.status_code == 200


def test_null_operators_need_no_value(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Missing comment", "comment", "is_null")]},
    )
    assert response.status_code == 200
    assert response.json()["rules"][0]["conditions"][0]["value"] is None


def test_an_unknown_operator_is_refused(admin_client, query):
    response = admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Nonsense", "amount", "approximately", "500")]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Flags on a run
# ---------------------------------------------------------------------------


def test_a_run_without_rules_carries_an_empty_outcome(admin_client, query):
    body = admin_client.post(f"/queries/{query['id']}/run").json()
    assert body["flags"] == {
        "flagged_count": 0,
        "rows": [],
        "rules": [],
        "warnings": [],
        # Part of the documented shape: a client reading it never has to test
        # for the key's presence before deciding whether to offer "restore".
        "dismissed_count": 0,
    }


def test_a_run_flags_the_matching_rows(admin_client, query):
    """The seeded rows are 10.5, 900.0, 12.0, 750.25, 20.0."""
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    body = admin_client.post(f"/queries/{query['id']}/run").json()

    assert body["flags"]["flagged_count"] == 2
    assert [row["index"] for row in body["flags"]["rows"]] == [1, 3]
    assert body["flags"]["rules"][0]["name"] == "Large"
    assert body["flags"]["rules"][0]["matched"] == 2
    # The rows themselves are untouched: flagging annotates, it does not filter.
    assert body["row_count"] == 5


def test_flags_work_on_a_result_with_no_flag_column(admin_client, sqlite_connection):
    """The feature's whole reason to exist.

    `day` and `comment` carry nothing a name-based heuristic could latch onto,
    so a rule is the only way these rows get flagged.
    """
    q = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, comment FROM txns ORDER BY id",
        name="no-flag-column",
    )
    admin_client.put(
        f"/queries/{q['id']}/flag-rules",
        json={"rules": [rule("Geo", "comment", "contains", "geo")]},
    )
    body = admin_client.post(f"/queries/{q['id']}/run").json()
    assert [row["index"] for row in body["flags"]["rows"]] == [3]


def test_a_rule_naming_a_missing_column_warns_instead_of_failing(admin_client, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Ghost", "not_a_column", "gt", "1")]},
    )
    response = admin_client.post(f"/queries/{query['id']}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 5
    assert body["flags"]["flagged_count"] == 0
    assert "not_a_column" in body["flags"]["warnings"][0]


def test_disabled_rules_do_not_flag(admin_client, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Off", "amount", "gt", "1", enabled=False)]},
    )
    body = admin_client.post(f"/queries/{query['id']}/run").json()
    assert body["flags"]["flagged_count"] == 0


# ---------------------------------------------------------------------------
# The hash, which is what makes a rule edit reach the browser
# ---------------------------------------------------------------------------


def test_editing_a_rule_changes_the_hash_although_the_data_did_not(admin_client, query):
    """Without this, polling reports `changed: false` and the edit never lands.

    The failure this guards against is nasty precisely because it looks like a
    save that did not happen: the rows are identical, so a hash over rows alone
    matches, the poll short-circuits, and reloading does not help because the
    cache agrees with itself.
    """
    before = admin_client.post(f"/queries/{query['id']}/run").json()["data_hash"]

    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    after = admin_client.post(f"/queries/{query['id']}/run").json()

    assert after["data_hash"] != before
    assert after["flags"]["flagged_count"] == 2


def test_saving_rules_invalidates_the_poll_cache(admin_client, query):
    admin_client.post(f"/queries/{query['id']}/run")
    cached = admin_client.get(f"/queries/{query['id']}/poll").json()
    assert cached["from_cache"] is True

    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    fresh = admin_client.get(f"/queries/{query['id']}/poll").json()
    assert fresh["from_cache"] is False
    assert fresh["flags"]["flagged_count"] == 2


# ---------------------------------------------------------------------------
# Preview with unsaved rules
# ---------------------------------------------------------------------------


def test_preview_evaluates_rules_that_were_never_saved(admin_client, sqlite_connection):
    response = admin_client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={
            "sql_text": "SELECT day, amount FROM txns ORDER BY id",
            "flag_rules": [rule("Trial", "amount", "gt", "500")],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["flags"]["flagged_count"] == 2
    assert body["flags"]["rules"][0]["name"] == "Trial"


def test_preview_without_rules_is_unchanged(admin_client, sqlite_connection):
    response = admin_client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT day FROM txns"},
    )
    assert response.json()["flags"]["flagged_count"] == 0


# ---------------------------------------------------------------------------
# The connection-level flagged view
# ---------------------------------------------------------------------------


def test_flagged_view_is_empty_for_a_connection_with_no_rules(
    admin_client, sqlite_connection, query
):
    body = admin_client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert body["queries"] == []
    assert body["flagged_count"] == 0


def test_flagged_view_reads_cache_and_runs_nothing(admin_client, sqlite_connection, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )

    # Nothing has run since the rules were saved, so there is no cache entry.
    cold = admin_client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert len(cold["queries"]) == 1
    assert cold["queries"][0]["stale"] is True
    assert cold["queries"][0]["flagged_count"] == 0

    admin_client.post(f"/queries/{query['id']}/run")

    warm = admin_client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert warm["queries"][0]["stale"] is False
    assert warm["queries"][0]["flagged_count"] == 2
    assert warm["flagged_count"] == 2


def test_flagged_rows_carry_their_values_and_the_rules_that_caught_them(
    admin_client, sqlite_connection, query
):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    admin_client.post(f"/queries/{query['id']}/run")

    section = admin_client.get(
        f"/connections/{sqlite_connection['id']}/flagged"
    ).json()["queries"][0]

    assert section["columns"] == ["day", "amount", "comment"]
    assert len(section["rows"]) == 2
    first = section["rows"][0]
    assert first["values"][1] == 900.0
    assert len(first["rule_ids"]) == 1
    assert section["rules"][0]["name"] == "Large"


def test_only_flagged_rows_are_returned_not_the_whole_result(
    admin_client, sqlite_connection, query
):
    """Five rows in the result, two of them flagged."""
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    admin_client.post(f"/queries/{query['id']}/run")
    section = admin_client.get(
        f"/connections/{sqlite_connection['id']}/flagged"
    ).json()["queries"][0]
    assert len(section["rows"]) == 2


def test_refresh_runs_the_queries(admin_client, sqlite_connection, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )

    response = admin_client.post(f"/connections/{sqlite_connection['id']}/flagged/refresh")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refreshed"] is True
    assert body["queries"][0]["stale"] is False
    assert body["flagged_count"] == 2


def test_flagged_view_spans_every_query_on_the_connection(admin_client, sqlite_connection):
    big = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns ORDER BY id",
        name="big",
    )
    quiet = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, comment FROM txns ORDER BY id",
        name="quiet",
    )
    admin_client.put(
        f"/queries/{big['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    admin_client.put(
        f"/queries/{quiet['id']}/flag-rules",
        json={"rules": [rule("No comment", "comment", "is_null")]},
    )

    body = admin_client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()

    by_name = {q["query_name"]: q for q in body["queries"]}
    assert by_name["big"]["flagged_count"] == 2
    assert by_name["quiet"]["flagged_count"] == 1
    assert body["flagged_count"] == 3


def test_a_broken_query_does_not_empty_the_whole_view(
    admin_client, sqlite_connection, session
):
    """One card's SQL failing must not hide the other card's flagged rows."""
    from app.models import SavedQuery

    good = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns ORDER BY id",
        name="good",
    )
    broken = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns ORDER BY id",
        name="broken",
    )
    for target in (good, broken):
        admin_client.put(
            f"/queries/{target['id']}/flag-rules",
            json={"rules": [rule("Large", "amount", "gt", "500")]},
        )

    # Break it behind the validator's back, the way a dropped table would.
    row = session.get(SavedQuery, broken["id"])
    row.sql_text = "SELECT day, amount FROM table_that_went_away"
    session.commit()

    body = admin_client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()

    by_name = {q["query_name"]: q for q in body["queries"]}
    assert by_name["good"]["flagged_count"] == 2
    assert by_name["broken"]["stale"] is True
    assert by_name["broken"]["error_code"] is not None
    assert body["flagged_count"] == 2


def test_refresh_is_bounded(admin_client, sqlite_connection, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FAE_FLAGGED_REFRESH_MAX_QUERIES", "1")
    get_settings.cache_clear()

    for name in ("one", "two"):
        q = make_query(
            admin_client,
            sqlite_connection["id"],
            "SELECT day, amount FROM txns ORDER BY id",
            name=name,
        )
        admin_client.put(
            f"/queries/{q['id']}/flag-rules",
            json={"rules": [rule("Large", "amount", "gt", "500")]},
        )

    body = admin_client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()
    assert body["refresh_truncated"] is True
    assert len(body["queries"]) == 1


def test_flagged_view_on_an_unknown_connection_is_a_404(admin_client):
    response = admin_client.get("/connections/nope/flagged")
    assert response.status_code == 404
    assert response.json()["error_code"] == "CONNECTION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Dismissing reviewed rows
#
# The flagged view is a review queue, so rows an analyst has cleared have to
# stop coming back. Nothing about a flagged row is stored - they are recomputed
# from the cached result on every load - so a dismissal records a hash of the
# row's values instead. That choice is what these tests are really about.
# ---------------------------------------------------------------------------


def _flagged(admin_client, connection_id):
    return admin_client.get(f"/connections/{connection_id}/flagged").json()["queries"][0]


@pytest.fixture
def flagged_section(admin_client, sqlite_connection, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    admin_client.post(f"/queries/{query['id']}/run")
    return _flagged(admin_client, sqlite_connection["id"])


def test_every_flagged_row_carries_a_fingerprint(flagged_section):
    # The client dismisses by fingerprint, never by index: an index is a
    # position in one run's result and points elsewhere after the next run.
    for row in flagged_section["rows"]:
        assert len(row["fingerprint"]) == 64
        assert set(row["fingerprint"]) <= set("0123456789abcdef")


def test_two_different_rows_fingerprint_differently(flagged_section):
    prints = {row["fingerprint"] for row in flagged_section["rows"]}
    assert len(prints) == len(flagged_section["rows"])


def test_a_dismissed_row_stops_appearing(
    admin_client, sqlite_connection, query, flagged_section
):
    victim = flagged_section["rows"][0]["fingerprint"]
    response = admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [victim]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == 1

    after = _flagged(admin_client, sqlite_connection["id"])
    assert [row["fingerprint"] for row in after["rows"]] == [
        flagged_section["rows"][1]["fingerprint"]
    ]


def test_the_count_follows_what_is_shown(admin_client, sqlite_connection, query, flagged_section):
    # A count that never moves however much of the queue is cleared is not a
    # queue length, it is decoration.
    assert flagged_section["flagged_count"] == 2
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    after = _flagged(admin_client, sqlite_connection["id"])
    assert after["flagged_count"] == 1
    assert after["dismissed_count"] == 1


def test_the_rule_legend_counts_only_what_survived(
    admin_client, sqlite_connection, query, flagged_section
):
    assert flagged_section["rules"][0]["matched"] == 2
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    after = _flagged(admin_client, sqlite_connection["id"])
    assert after["rules"][0]["matched"] == 1


def test_dismissing_every_row_empties_the_section(
    admin_client, sqlite_connection, query, flagged_section
):
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [r["fingerprint"] for r in flagged_section["rows"]]},
    )
    after = _flagged(admin_client, sqlite_connection["id"])
    assert after["rows"] == []
    assert after["flagged_count"] == 0
    assert after["dismissed_count"] == 2


def test_a_dismissal_survives_a_refresh(
    admin_client, sqlite_connection, query, flagged_section
):
    # The whole point: re-running the query must not resurrect reviewed rows.
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    refreshed = admin_client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()
    assert refreshed["queries"][0]["flagged_count"] == 1


def test_dismissing_twice_is_not_an_error(admin_client, query, flagged_section):
    body = {"fingerprints": [flagged_section["rows"][0]["fingerprint"]]}
    assert admin_client.post(f"/queries/{query['id']}/flag-dismissals", json=body).json()[
        "changed"
    ] == 1
    # Two tabs open on the same queue is normal use, not a conflict.
    second = admin_client.post(f"/queries/{query['id']}/flag-dismissals", json=body)
    assert second.status_code == 200
    assert second.json()["changed"] == 0


def test_restoring_lifts_the_suppression_and_the_row_returns_on_the_next_run(
    admin_client, sqlite_connection, query, flagged_section
):
    """Restoring is not an undelete.

    Dismissing removes the stored finding, which is what was asked for, so
    there is nothing to put back: what a restore does is stop suppressing the
    row, and the next run flags it again. Until that run the queue is honestly
    empty of it.
    """
    victim = flagged_section["rows"][0]["fingerprint"]
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals", json={"fingerprints": [victim]}
    )
    response = admin_client.delete(f"/queries/{query['id']}/flag-dismissals")
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == 1

    # Suppression lifted, but nothing has re-run yet.
    lifted = _flagged(admin_client, sqlite_connection["id"])
    assert lifted["flagged_count"] == 1
    assert lifted["dismissed_count"] == 0

    admin_client.post(f"/queries/{query['id']}/run")
    after = _flagged(admin_client, sqlite_connection["id"])
    assert after["flagged_count"] == 2


def test_restoring_one_named_row_leaves_the_others_dismissed(
    admin_client, sqlite_connection, query, flagged_section
):
    prints = [row["fingerprint"] for row in flagged_section["rows"]]
    admin_client.post(f"/queries/{query['id']}/flag-dismissals", json={"fingerprints": prints})
    admin_client.delete(
        f"/queries/{query['id']}/flag-dismissals", params={"fingerprint": [prints[0]]}
    )
    # Only the named row is un-suppressed, and it comes back when the query
    # next runs; the other stays dismissed.
    admin_client.post(f"/queries/{query['id']}/run")
    after = _flagged(admin_client, sqlite_connection["id"])
    assert [row["fingerprint"] for row in after["rows"]] == [prints[0]]


def test_a_dismissal_is_scoped_to_its_query(admin_client, sqlite_connection, query, flagged_section):
    # The same values flagged by two different queries are two findings.
    twin = make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, amount, comment FROM txns",
        name="twin",
    )
    admin_client.put(
        f"/queries/{twin['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    admin_client.post(f"/queries/{twin['id']}/run")
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [r["fingerprint"] for r in flagged_section["rows"]]},
    )

    sections = admin_client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    by_name = {s["query_name"]: s for s in sections["queries"]}
    assert by_name["q"]["flagged_count"] == 0
    assert by_name["twin"]["flagged_count"] == 2


def test_a_fingerprint_that_is_not_a_hash_is_refused(admin_client, query):
    response = admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": ["'; DROP TABLE flag_dismissals; --"]},
    )
    assert response.status_code == 422


def test_dismissing_on_an_unknown_query_is_a_404(admin_client):
    response = admin_client.post(
        "/queries/does-not-exist/flag-dismissals", json={"fingerprints": []}
    )
    assert response.status_code == 404


def test_deleting_the_query_takes_its_dismissals_with_it(
    admin_client, sqlite_connection, query, flagged_section
):
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    assert admin_client.delete(f"/queries/{query['id']}").status_code == 204
    # A dismissal describes rows a deleted query can no longer produce.
    from app.models import FlagDismissal
    from app.db.app_state import get_engine
    from sqlalchemy.orm import Session

    with Session(get_engine()) as session:
        assert session.query(FlagDismissal).count() == 0


# ---------------------------------------------------------------------------
# A dismissed row stops being marked on its chart too
#
# The flagged view is one consumer of a query's flag outcome; every chart
# polling that query is another. A card still painting a row red after the
# analyst reviewed it is telling them there is work left that they have
# already done.
# ---------------------------------------------------------------------------


def test_a_dismissed_row_is_not_flagged_in_the_run_payload(
    admin_client, query, flagged_section
):
    victim = flagged_section["rows"][0]["fingerprint"]
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals", json={"fingerprints": [victim]}
    )

    run = admin_client.post(f"/queries/{query['id']}/run").json()
    assert run["flags"]["flagged_count"] == 1
    assert victim not in [row["fingerprint"] for row in run["flags"]["rows"]]
    # The rows themselves are untouched: only the flag on one of them is gone.
    assert len(run["rows"]) == 5


def test_the_run_payload_carries_a_fingerprint_per_flagged_row(admin_client, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    run = admin_client.post(f"/queries/{query['id']}/run").json()
    for row in run["flags"]["rows"]:
        assert len(row["fingerprint"]) == 64


def test_dismissing_moves_the_hash_so_a_polling_card_notices(
    admin_client, query, flagged_section
):
    """The part that is easy to miss.

    A card polls with "anything changed since this hash". Dismissing changes
    what the card should draw without changing a single value in the result,
    so a hash over the data alone would answer "unchanged" and the row would
    stay marked until something else happened to move it.
    """
    before = admin_client.get(f"/queries/{query['id']}/poll").json()
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    after = admin_client.get(f"/queries/{query['id']}/poll").json()

    assert after["data_hash"] != before["data_hash"]
    assert after["flags"]["flagged_count"] == 1


def test_polling_with_the_new_hash_then_reports_unchanged(
    admin_client, query, flagged_section
):
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    current = admin_client.get(f"/queries/{query['id']}/poll").json()["data_hash"]
    again = admin_client.get(
        f"/queries/{query['id']}/poll", params={"since_hash": current}
    ).json()
    assert again["changed"] is False


def test_dismissing_does_not_re_run_the_query(admin_client, query, flagged_section):
    """A dismissal must be free of the target database.

    Invalidating the cache instead would also blank the flagged section, which
    reads that cache, until someone hit refresh.
    """
    before = len(admin_client.get(f"/queries/{query['id']}/logs").json())
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    served = admin_client.get(f"/queries/{query['id']}/poll").json()

    assert served["from_cache"] is True
    assert len(admin_client.get(f"/queries/{query['id']}/logs").json()) == before


def test_restoring_puts_the_mark_back_on_the_chart(admin_client, query, flagged_section):
    victim = flagged_section["rows"][0]["fingerprint"]
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals", json={"fingerprints": [victim]}
    )
    assert admin_client.get(f"/queries/{query['id']}/poll").json()["flags"]["flagged_count"] == 1

    admin_client.delete(f"/queries/{query['id']}/flag-dismissals")
    restored = admin_client.get(f"/queries/{query['id']}/poll").json()
    assert restored["flags"]["flagged_count"] == 2
    assert victim in [row["fingerprint"] for row in restored["flags"]["rows"]]


def test_the_rule_legend_on_a_run_counts_only_what_is_still_flagged(
    admin_client, query, flagged_section
):
    admin_client.post(
        f"/queries/{query['id']}/flag-dismissals",
        json={"fingerprints": [flagged_section["rows"][0]["fingerprint"]]},
    )
    run = admin_client.post(f"/queries/{query['id']}/run").json()
    assert run["flags"]["rules"][0]["matched"] == 1


def test_a_query_with_no_dismissals_is_untouched(admin_client, query):
    admin_client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    first = admin_client.post(f"/queries/{query['id']}/run").json()
    second = admin_client.post(f"/queries/{query['id']}/run").json()
    # No dismissals means the hash must be exactly what it always was, or every
    # card on the dashboard would redraw for nothing.
    assert first["data_hash"] == second["data_hash"]
    assert "+d" not in first["data_hash"]
