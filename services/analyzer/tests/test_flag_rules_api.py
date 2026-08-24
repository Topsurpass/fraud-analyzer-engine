"""Flag-rule storage, flagged runs, and the per-connection flagged view.

The target fixture (``tests/conftest.py``) is deliberately a schema with no
flag column of any kind: `txns` has day, user_id, amount, flagged, comment. The
tests here mostly select columns that carry no hint of fraud, because the whole
point of the feature is that flagging must work on a schema the engine knows
nothing about.
"""

from __future__ import annotations

import pytest


def make_query(client, connection_id, sql, name="q", **extra):
    response = client.post(
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
def query(client, sqlite_connection):
    return make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, amount, comment FROM txns ORDER BY id",
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_a_new_query_has_no_rules(client, query):
    response = client.get(f"/queries/{query['id']}/flag-rules")
    assert response.status_code == 200
    assert response.json() == {"query_id": query["id"], "rules": []}


def test_rules_round_trip(client, query):
    payload = {"rules": [rule("Large", "amount", "gt", "500")]}
    response = client.put(f"/queries/{query['id']}/flag-rules", json=payload)
    assert response.status_code == 200, response.text

    stored = response.json()["rules"]
    assert len(stored) == 1
    assert stored[0]["name"] == "Large"
    assert stored[0]["severity"] == "high"
    assert stored[0]["position"] == 0
    assert stored[0]["conditions"][0]["column_name"] == "amount"
    assert stored[0]["conditions"][0]["operator"] == "gt"

    again = client.get(f"/queries/{query['id']}/flag-rules").json()
    assert again["rules"] == stored


def test_put_replaces_rather_than_appends(client, query):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("First", "amount", "gt", "1")]},
    )
    response = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Second", "amount", "gt", "2")]},
    )
    names = [r["name"] for r in response.json()["rules"]]
    assert names == ["Second"]


def test_an_empty_list_clears_every_rule(client, query):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    response = client.put(f"/queries/{query['id']}/flag-rules", json={"rules": []})
    assert response.json()["rules"] == []


def test_position_follows_list_order(client, query):
    response = client.put(
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


def test_duplicate_rule_names_are_refused(client, query):
    response = client.put(
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


def test_rules_on_an_unknown_query_are_a_404(client):
    response = client.get("/queries/does-not-exist/flag-rules")
    assert response.status_code == 404
    assert response.json()["error_code"] == "QUERY_NOT_FOUND"


def test_deleting_a_query_takes_its_rules_with_it(client, query, session):
    from app.models import FlagCondition, FlagRule

    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    assert session.query(FlagRule).count() == 1
    assert session.query(FlagCondition).count() == 1

    assert client.delete(f"/queries/{query['id']}").status_code == 204

    session.expire_all()
    assert session.query(FlagRule).count() == 0
    # The condition must go too, or the next query to reuse the id inherits it.
    assert session.query(FlagCondition).count() == 0


# ---------------------------------------------------------------------------
# Validation of rule shape
# ---------------------------------------------------------------------------


def test_a_rule_needs_at_least_one_condition(client, query):
    response = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [{"name": "Empty", "severity": "low", "conditions": []}]},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("operator", ["gt", "eq", "contains", "in", "starts_with"])
def test_operators_that_read_a_value_must_be_given_one(client, query, operator):
    response = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("No value", "amount", operator)]},
    )
    assert response.status_code == 422


def test_between_needs_both_bounds(client, query):
    response = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Half range", "amount", "between", "100")]},
    )
    assert response.status_code == 422

    ok = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Range", "amount", "between", "100", "900")]},
    )
    assert ok.status_code == 200


def test_null_operators_need_no_value(client, query):
    response = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Missing comment", "comment", "is_null")]},
    )
    assert response.status_code == 200
    assert response.json()["rules"][0]["conditions"][0]["value"] is None


def test_an_unknown_operator_is_refused(client, query):
    response = client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Nonsense", "amount", "approximately", "500")]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Flags on a run
# ---------------------------------------------------------------------------


def test_a_run_without_rules_carries_an_empty_outcome(client, query):
    body = client.post(f"/queries/{query['id']}/run").json()
    assert body["flags"] == {
        "flagged_count": 0,
        "rows": [],
        "rules": [],
        "warnings": [],
    }


def test_a_run_flags_the_matching_rows(client, query):
    """The seeded rows are 10.5, 900.0, 12.0, 750.25, 20.0."""
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    body = client.post(f"/queries/{query['id']}/run").json()

    assert body["flags"]["flagged_count"] == 2
    assert [row["index"] for row in body["flags"]["rows"]] == [1, 3]
    assert body["flags"]["rules"][0]["name"] == "Large"
    assert body["flags"]["rules"][0]["matched"] == 2
    # The rows themselves are untouched: flagging annotates, it does not filter.
    assert body["row_count"] == 5


def test_flags_work_on_a_result_with_no_flag_column(client, sqlite_connection):
    """The feature's whole reason to exist.

    `day` and `comment` carry nothing a name-based heuristic could latch onto,
    so a rule is the only way these rows get flagged.
    """
    q = make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, comment FROM txns ORDER BY id",
        name="no-flag-column",
    )
    client.put(
        f"/queries/{q['id']}/flag-rules",
        json={"rules": [rule("Geo", "comment", "contains", "geo")]},
    )
    body = client.post(f"/queries/{q['id']}/run").json()
    assert [row["index"] for row in body["flags"]["rows"]] == [3]


def test_a_rule_naming_a_missing_column_warns_instead_of_failing(client, query):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Ghost", "not_a_column", "gt", "1")]},
    )
    response = client.post(f"/queries/{query['id']}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 5
    assert body["flags"]["flagged_count"] == 0
    assert "not_a_column" in body["flags"]["warnings"][0]


def test_disabled_rules_do_not_flag(client, query):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Off", "amount", "gt", "1", enabled=False)]},
    )
    body = client.post(f"/queries/{query['id']}/run").json()
    assert body["flags"]["flagged_count"] == 0


# ---------------------------------------------------------------------------
# The hash, which is what makes a rule edit reach the browser
# ---------------------------------------------------------------------------


def test_editing_a_rule_changes_the_hash_although_the_data_did_not(client, query):
    """Without this, polling reports `changed: false` and the edit never lands.

    The failure this guards against is nasty precisely because it looks like a
    save that did not happen: the rows are identical, so a hash over rows alone
    matches, the poll short-circuits, and reloading does not help because the
    cache agrees with itself.
    """
    before = client.post(f"/queries/{query['id']}/run").json()["data_hash"]

    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    after = client.post(f"/queries/{query['id']}/run").json()

    assert after["data_hash"] != before
    assert after["flags"]["flagged_count"] == 2


def test_saving_rules_invalidates_the_poll_cache(client, query):
    client.post(f"/queries/{query['id']}/run")
    cached = client.get(f"/queries/{query['id']}/poll").json()
    assert cached["from_cache"] is True

    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    fresh = client.get(f"/queries/{query['id']}/poll").json()
    assert fresh["from_cache"] is False
    assert fresh["flags"]["flagged_count"] == 2


# ---------------------------------------------------------------------------
# Preview with unsaved rules
# ---------------------------------------------------------------------------


def test_preview_evaluates_rules_that_were_never_saved(client, sqlite_connection):
    response = client.post(
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


def test_preview_without_rules_is_unchanged(client, sqlite_connection):
    response = client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={"sql_text": "SELECT day FROM txns"},
    )
    assert response.json()["flags"]["flagged_count"] == 0


# ---------------------------------------------------------------------------
# The connection-level flagged view
# ---------------------------------------------------------------------------


def test_flagged_view_is_empty_for_a_connection_with_no_rules(
    client, sqlite_connection, query
):
    body = client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert body["queries"] == []
    assert body["flagged_count"] == 0


def test_flagged_view_reads_cache_and_runs_nothing(client, sqlite_connection, query):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )

    # Nothing has run since the rules were saved, so there is no cache entry.
    cold = client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert len(cold["queries"]) == 1
    assert cold["queries"][0]["stale"] is True
    assert cold["queries"][0]["flagged_count"] == 0

    client.post(f"/queries/{query['id']}/run")

    warm = client.get(f"/connections/{sqlite_connection['id']}/flagged").json()
    assert warm["queries"][0]["stale"] is False
    assert warm["queries"][0]["flagged_count"] == 2
    assert warm["flagged_count"] == 2


def test_flagged_rows_carry_their_values_and_the_rules_that_caught_them(
    client, sqlite_connection, query
):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    client.post(f"/queries/{query['id']}/run")

    section = client.get(
        f"/connections/{sqlite_connection['id']}/flagged"
    ).json()["queries"][0]

    assert section["columns"] == ["day", "amount", "comment"]
    assert len(section["rows"]) == 2
    first = section["rows"][0]
    assert first["values"][1] == 900.0
    assert len(first["rule_ids"]) == 1
    assert section["rules"][0]["name"] == "Large"


def test_only_flagged_rows_are_returned_not_the_whole_result(
    client, sqlite_connection, query
):
    """Five rows in the result, two of them flagged."""
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    client.post(f"/queries/{query['id']}/run")
    section = client.get(
        f"/connections/{sqlite_connection['id']}/flagged"
    ).json()["queries"][0]
    assert len(section["rows"]) == 2


def test_refresh_runs_the_queries(client, sqlite_connection, query):
    client.put(
        f"/queries/{query['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )

    response = client.post(f"/connections/{sqlite_connection['id']}/flagged/refresh")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refreshed"] is True
    assert body["queries"][0]["stale"] is False
    assert body["flagged_count"] == 2


def test_flagged_view_spans_every_query_on_the_connection(client, sqlite_connection):
    big = make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns ORDER BY id",
        name="big",
    )
    quiet = make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, comment FROM txns ORDER BY id",
        name="quiet",
    )
    client.put(
        f"/queries/{big['id']}/flag-rules",
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    client.put(
        f"/queries/{quiet['id']}/flag-rules",
        json={"rules": [rule("No comment", "comment", "is_null")]},
    )

    body = client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()

    by_name = {q["query_name"]: q for q in body["queries"]}
    assert by_name["big"]["flagged_count"] == 2
    assert by_name["quiet"]["flagged_count"] == 1
    assert body["flagged_count"] == 3


def test_a_broken_query_does_not_empty_the_whole_view(
    client, sqlite_connection, session
):
    """One card's SQL failing must not hide the other card's flagged rows."""
    from app.models import SavedQuery

    good = make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns ORDER BY id",
        name="good",
    )
    broken = make_query(
        client,
        sqlite_connection["id"],
        "SELECT day, amount FROM txns ORDER BY id",
        name="broken",
    )
    for target in (good, broken):
        client.put(
            f"/queries/{target['id']}/flag-rules",
            json={"rules": [rule("Large", "amount", "gt", "500")]},
        )

    # Break it behind the validator's back, the way a dropped table would.
    row = session.get(SavedQuery, broken["id"])
    row.sql_text = "SELECT day, amount FROM table_that_went_away"
    session.commit()

    body = client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()

    by_name = {q["query_name"]: q for q in body["queries"]}
    assert by_name["good"]["flagged_count"] == 2
    assert by_name["broken"]["stale"] is True
    assert by_name["broken"]["error_code"] is not None
    assert body["flagged_count"] == 2


def test_refresh_is_bounded(client, sqlite_connection, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FAE_FLAGGED_REFRESH_MAX_QUERIES", "1")
    get_settings.cache_clear()

    for name in ("one", "two"):
        q = make_query(
            client,
            sqlite_connection["id"],
            "SELECT day, amount FROM txns ORDER BY id",
            name=name,
        )
        client.put(
            f"/queries/{q['id']}/flag-rules",
            json={"rules": [rule("Large", "amount", "gt", "500")]},
        )

    body = client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh"
    ).json()
    assert body["refresh_truncated"] is True
    assert len(body["queries"]) == 1


def test_flagged_view_on_an_unknown_connection_is_a_404(client):
    response = client.get("/connections/nope/flagged")
    assert response.status_code == 404
    assert response.json()["error_code"] == "CONNECTION_NOT_FOUND"
