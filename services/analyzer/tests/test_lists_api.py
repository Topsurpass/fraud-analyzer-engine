"""Named lists over HTTP, and their effect on flag rules.

The target fixture's `txns` has user_id 1, 1, 2, 3, 3 and amounts 10.5, 900.0,
12.0, 750.25, 20.0, so a list of user ids picks rows deterministically.
"""

from __future__ import annotations

import pytest

from app.enums import UserRole
from tests.test_auth_api import login, make_user
from tests.test_flag_rules_api import make_query, rule


def make_list(client, name="Watch", items=("1", "3"), description=None, **extra):
    response = client.post(
        "/lists",
        json={"name": name, "description": description, "items": list(items), **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def list_rule(name, column, operator, list_id, severity="high"):
    return {
        "name": name,
        "severity": severity,
        "enabled": True,
        "conditions": [{"column_name": column, "operator": operator, "list_id": list_id}],
    }


@pytest.fixture
def analyst_client(client, app_db):
    make_user(email="analyst-a@example.com", role=UserRole.ANALYST)
    token = login(client, email="analyst-a@example.com").json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def other_analyst_headers(client, app_db):
    make_user(email="analyst-b@example.com", role=UserRole.ANALYST)
    token = login(client, email="analyst-b@example.com").json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def query(admin_client, sqlite_connection):
    return make_query(
        admin_client,
        sqlite_connection["id"],
        "SELECT day, user_id, amount, comment FROM txns ORDER BY id",
    )


def put_rules(client, query_id, rules):
    return client.put(f"/queries/{query_id}/flag-rules", json={"rules": rules})


# --- CRUD --------------------------------------------------------------------


def test_create_returns_the_documented_shape(admin_client):
    body = make_list(admin_client, "Blocked terminals", ["T1", "T2"], "Seen in chargebacks")

    assert set(body) == {
        "id", "name", "description", "item_count", "rule_count", "created_by",
        "created_at", "updated_at", "items", "received", "kept", "duplicates_dropped",
    }
    assert body["name"] == "Blocked terminals"
    assert body["description"] == "Seen in chargebacks"
    assert body["items"] == ["T1", "T2"]
    assert (body["item_count"], body["rule_count"]) == (2, 0)
    assert (body["received"], body["kept"], body["duplicates_dropped"]) == (2, 2, 0)
    assert body["created_by"]


def test_get_returns_items_and_the_list_endpoint_does_not(admin_client):
    created = make_list(admin_client, items=["x", "y", "z"])

    one = admin_client.get(f"/lists/{created['id']}").json()
    assert one["items"] == ["x", "y", "z"]
    assert "received" not in one

    listing = admin_client.get("/lists").json()
    assert len(listing) == 1
    assert "items" not in listing[0]
    assert listing[0]["item_count"] == 3


def test_listing_is_sorted_by_name_ignoring_case(admin_client):
    for name in ["beta", "Alpha", "Gamma"]:
        make_list(admin_client, name, ["1"])
    assert [row["name"] for row in admin_client.get("/lists").json()] == [
        "Alpha", "beta", "Gamma",
    ]


def test_put_replaces_name_description_and_items(admin_client):
    created = make_list(admin_client, "Old", ["a", "b"], "old text")

    response = admin_client.put(
        f"/lists/{created['id']}",
        json={"name": "New", "description": None, "items": ["c"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["description"], body["items"]) == ("New", None, ["c"])
    assert body["updated_at"] >= created["updated_at"]
    assert admin_client.get(f"/lists/{created['id']}").json()["items"] == ["c"]


def test_delete_an_unused_list(admin_client):
    created = make_list(admin_client)
    assert admin_client.delete(f"/lists/{created['id']}").status_code == 204
    assert admin_client.get(f"/lists/{created['id']}").status_code == 404
    assert admin_client.get("/lists").json() == []


def test_unknown_list_is_404_with_its_code(admin_client):
    for response in (
        admin_client.get("/lists/nope"),
        admin_client.put("/lists/nope", json={"name": "x", "items": []}),
        admin_client.delete("/lists/nope"),
    ):
        assert response.status_code == 404
        assert response.json()["error_code"] == "LIST_NOT_FOUND"


def test_an_empty_list_is_allowed(admin_client):
    body = make_list(admin_client, items=[])
    assert body["item_count"] == 0


def test_description_is_trimmed_and_blank_becomes_null(admin_client):
    assert make_list(admin_client, "A", ["1"], "   ")["description"] is None
    assert make_list(admin_client, "B", ["1"], "  hi ")["description"] == "hi"


# --- Names -------------------------------------------------------------------


def test_duplicate_name_ignoring_case_is_a_409(admin_client):
    make_list(admin_client, "Watchlist")
    response = admin_client.post("/lists", json={"name": "WATCHLIST", "items": []})
    assert response.status_code == 409
    assert response.json()["error_code"] == "LIST_NAME_TAKEN"


def test_a_name_is_trimmed_before_the_uniqueness_check(admin_client):
    make_list(admin_client, "Watchlist")
    response = admin_client.post("/lists", json={"name": "  watchlist  ", "items": []})
    assert response.status_code == 409


def test_renaming_onto_another_list_is_refused_but_onto_itself_is_fine(admin_client):
    first = make_list(admin_client, "First")
    second = make_list(admin_client, "Second")

    clash = admin_client.put(f"/lists/{second['id']}", json={"name": "first", "items": []})
    assert clash.status_code == 409

    recase = admin_client.put(f"/lists/{first['id']}", json={"name": "FIRST", "items": []})
    assert recase.status_code == 200
    assert recase.json()["name"] == "FIRST"


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_name_is_refused(admin_client, name):
    assert admin_client.post("/lists", json={"name": name, "items": []}).status_code == 422


def test_an_overlong_name_or_description_is_refused(admin_client):
    assert admin_client.post("/lists", json={"name": "n" * 201, "items": []}).status_code == 422
    assert (
        admin_client.post(
            "/lists", json={"name": "ok", "description": "d" * 1001, "items": []}
        ).status_code
        == 422
    )


# --- Items -------------------------------------------------------------------


def test_duplicates_are_dropped_by_match_key_and_reported(admin_client):
    body = make_list(admin_client, items=["A1", "a1 ", "2", "2.0", " ", "", "b"])
    assert body["items"] == ["A1", "2", "b"]
    assert (body["received"], body["kept"], body["duplicates_dropped"]) == (7, 3, 4)


def test_items_are_trimmed_and_keep_their_order(admin_client):
    body = make_list(admin_client, items=["  z ", "a", "m"])
    assert body["items"] == ["z", "a", "m"]


def test_an_item_over_500_characters_is_refused(admin_client):
    ok = admin_client.post("/lists", json={"name": "ok", "items": ["x" * 500]})
    assert ok.status_code == 201
    bad = admin_client.post("/lists", json={"name": "bad", "items": ["x" * 501]})
    assert bad.status_code == 422


def test_the_item_count_limit_is_enforced(admin_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FAE_MAX_LIST_ITEMS", "3")
    get_settings.cache_clear()

    assert admin_client.post("/lists", json={"name": "ok", "items": list("abc")}).status_code == 201
    over = admin_client.post("/lists", json={"name": "over", "items": list("abcd")})
    assert over.status_code == 422
    assert over.json()["error_code"] == "REQUEST_VALIDATION_ERROR"
    assert over.json()["detail"]["max_items"] == 3
    # Nothing was created by the refused request.
    assert [row["name"] for row in admin_client.get("/lists").json()] == ["ok"]

    put = admin_client.put(
        f"/lists/{admin_client.get('/lists').json()[0]['id']}",
        json={"name": "ok", "items": list("abcd")},
    )
    assert put.status_code == 422


def test_duplicates_count_toward_the_limit_before_they_are_dropped(admin_client, monkeypatch):
    """The limit protects the request, so it applies to what was sent."""
    from app.config import get_settings

    monkeypatch.setenv("FAE_MAX_LIST_ITEMS", "2")
    get_settings.cache_clear()
    assert admin_client.post("/lists", json={"name": "d", "items": ["a", "a", "a"]}).status_code == 422


def test_a_large_list_round_trips(admin_client):
    items = [f"ACCT-{i:05d}" for i in range(20_000)]
    body = make_list(admin_client, "Big", items)
    assert body["kept"] == 20_000
    assert admin_client.get(f"/lists/{body['id']}").json()["items"] == items


# --- Permissions -------------------------------------------------------------


def test_any_signed_in_user_can_read_and_list(client, admin_client, analyst_client):
    created = make_list(admin_client)
    listed = client.get("/lists", headers=analyst_client)
    assert [row["id"] for row in listed.json()] == [created["id"]]
    assert client.get(f"/lists/{created['id']}", headers=analyst_client).status_code == 200


def test_anonymous_requests_are_refused(client):
    assert client.get("/lists").status_code == 401
    assert client.post("/lists", json={"name": "x", "items": []}).status_code == 401


def test_an_analyst_can_create_and_change_their_own_list(client, analyst_client):
    created = client.post("/lists", json={"name": "Mine", "items": ["1"]}, headers=analyst_client)
    assert created.status_code == 201
    mine = created.json()["id"]

    assert client.put(
        f"/lists/{mine}", json={"name": "Mine", "items": ["1", "2"]}, headers=analyst_client
    ).status_code == 200
    assert client.delete(f"/lists/{mine}", headers=analyst_client).status_code == 204


def test_a_non_owner_analyst_cannot_edit_or_delete_but_can_read(
    client, analyst_client, other_analyst_headers
):
    mine = client.post(
        "/lists", json={"name": "Mine", "items": ["1"]}, headers=analyst_client
    ).json()["id"]

    edit = client.put(
        f"/lists/{mine}", json={"name": "Hijack", "items": []}, headers=other_analyst_headers
    )
    assert edit.status_code == 403
    assert edit.json()["error_code"] == "FORBIDDEN"
    assert client.delete(f"/lists/{mine}", headers=other_analyst_headers).status_code == 403

    still = client.get(f"/lists/{mine}", headers=other_analyst_headers).json()
    assert (still["name"], still["items"]) == ("Mine", ["1"])


def test_an_admin_can_edit_and_delete_anyones_list(client, admin_client, analyst_client):
    mine = client.post(
        "/lists", json={"name": "Mine", "items": ["1"]}, headers=analyst_client
    ).json()["id"]

    # admin_client already carries the admin token in its default headers.
    assert admin_client.put(
        f"/lists/{mine}", json={"name": "Renamed", "items": ["9"]}
    ).status_code == 200
    assert admin_client.delete(f"/lists/{mine}").status_code == 204


def test_a_list_whose_creator_is_gone_is_admin_only(client, admin_client, analyst_client, session):
    from app.features.lists.models import ItemList

    mine = client.post(
        "/lists", json={"name": "Orphan", "items": ["1"]}, headers=analyst_client
    ).json()["id"]
    item_list = session.get(ItemList, mine)
    item_list.created_by = None
    session.commit()

    assert client.put(
        f"/lists/{mine}", json={"name": "Orphan", "items": []}, headers=analyst_client
    ).status_code == 403
    assert admin_client.put(f"/lists/{mine}", json={"name": "Orphan", "items": []}).status_code == 200


# --- Rules using lists -------------------------------------------------------


def test_rule_round_trip_carries_list_id_and_list_name(admin_client, query):
    watch = make_list(admin_client, "Watch")
    response = put_rules(admin_client, query["id"], [list_rule("On list", "user_id", "in_list", watch["id"])])
    assert response.status_code == 200, response.text

    condition = response.json()["rules"][0]["conditions"][0]
    assert condition["operator"] == "in_list"
    assert condition["list_id"] == watch["id"]
    assert condition["list_name"] == "Watch"
    assert condition["value"] is None and condition["value2"] is None

    again = admin_client.get(f"/queries/{query['id']}/flag-rules").json()
    assert again["rules"][0]["conditions"][0]["list_name"] == "Watch"


def test_a_renamed_list_shows_its_new_name_in_the_rule(admin_client, query):
    watch = make_list(admin_client, "Watch")
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    admin_client.put(f"/lists/{watch['id']}", json={"name": "Renamed", "items": ["1"]})

    condition = admin_client.get(f"/queries/{query['id']}/flag-rules").json()["rules"][0]["conditions"][0]
    assert condition["list_name"] == "Renamed"


def test_a_plain_condition_reports_no_list(admin_client, query):
    put_rules(admin_client, query["id"], [rule("Big", "amount", "gt", "500")])
    condition = admin_client.get(f"/queries/{query['id']}/flag-rules").json()["rules"][0]["conditions"][0]
    assert condition["list_id"] is None and condition["list_name"] is None


@pytest.mark.parametrize("operator", ["in_list", "not_in_list"])
def test_a_list_operator_without_a_list_id_is_422(admin_client, query, operator):
    response = put_rules(admin_client, query["id"], [rule("R", "user_id", operator, "1")])
    assert response.status_code == 422


def test_an_unknown_list_id_is_refused_and_saves_nothing(admin_client, query):
    put_rules(admin_client, query["id"], [rule("Keep me", "amount", "gt", "1")])

    response = put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", "no-such-list")])

    assert response.status_code == 404
    assert response.json()["error_code"] == "LIST_NOT_FOUND"
    assert response.json()["detail"]["list_ids"] == ["no-such-list"]
    kept = admin_client.get(f"/queries/{query['id']}/flag-rules").json()["rules"]
    assert [r["name"] for r in kept] == ["Keep me"]


def test_list_id_on_a_non_list_operator_is_cleared(admin_client, query):
    watch = make_list(admin_client)
    payload = rule("R", "amount", "gt", "5")
    payload["conditions"][0]["list_id"] = watch["id"]

    stored = put_rules(admin_client, query["id"], [payload]).json()["rules"][0]["conditions"][0]

    assert stored["list_id"] is None
    assert admin_client.get("/lists").json()[0]["rule_count"] == 0


def test_value_on_a_list_operator_is_cleared(admin_client, query):
    watch = make_list(admin_client)
    payload = list_rule("R", "user_id", "in_list", watch["id"])
    payload["conditions"][0]["value"] = "stray"

    stored = put_rules(admin_client, query["id"], [payload]).json()["rules"][0]["conditions"][0]
    assert stored["value"] is None


def test_changing_a_condition_from_list_to_inline_frees_the_list(admin_client, query):
    watch = make_list(admin_client)
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    put_rules(admin_client, query["id"], [rule("R", "user_id", "in", "1,3")])
    assert admin_client.delete(f"/lists/{watch['id']}").status_code == 204


# --- Flags: parity, cache, preview ------------------------------------------


def run_flagged(client, query_id):
    return [row["index"] for row in client.post(f"/queries/{query_id}/run").json()["flags"]["rows"]]


def test_in_list_flags_the_same_rows_as_typing_the_items_inline(admin_client, sqlite_connection):
    inline = make_query(admin_client, sqlite_connection["id"], "SELECT user_id FROM txns ORDER BY id", name="inline")
    listed = make_query(admin_client, sqlite_connection["id"], "SELECT user_id FROM txns ORDER BY id", name="listed")
    watch = make_list(admin_client, items=["1", "3"])

    put_rules(admin_client, inline["id"], [rule("R", "user_id", "in", "1,3")])
    put_rules(admin_client, listed["id"], [list_rule("R", "user_id", "in_list", watch["id"])])

    assert run_flagged(admin_client, listed["id"]) == run_flagged(admin_client, inline["id"]) == [0, 1, 3, 4]


def test_not_in_list_is_the_complement(admin_client, query):
    watch = make_list(admin_client, items=["1", "3"])
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "not_in_list", watch["id"])])
    assert run_flagged(admin_client, query["id"]) == [2]


def test_null_cells_match_neither_list_operator(admin_client, query):
    """`comment` is NULL on row 2 (id 3)."""
    watch = make_list(admin_client, items=["ok", "velocity"])
    put_rules(admin_client, query["id"], [list_rule("In", "comment", "in_list", watch["id"])])
    assert run_flagged(admin_client, query["id"]) == [0, 1, 4]
    put_rules(admin_client, query["id"], [list_rule("Out", "comment", "not_in_list", watch["id"])])
    assert run_flagged(admin_client, query["id"]) == [3]


def test_matching_ignores_case_and_spaces_but_compares_numbers_as_numbers(admin_client, query):
    watch = make_list(admin_client, items=["OK ", " VELOCITY"])
    put_rules(admin_client, query["id"], [list_rule("R", "comment", "in_list", watch["id"])])
    assert run_flagged(admin_client, query["id"]) == [0, 1, 4]

    numbers = make_list(admin_client, "Numbers", items=["1.0", "3.00"])
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", numbers["id"])])
    assert run_flagged(admin_client, query["id"]) == [0, 1, 3, 4]


def test_editing_the_list_changes_the_next_poll_with_no_rule_edit(admin_client, query):
    watch = make_list(admin_client, items=["1"])
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    admin_client.post(f"/queries/{query['id']}/run")
    before = admin_client.get(f"/queries/{query['id']}/poll").json()
    assert before["from_cache"] is True
    assert before["flags"]["flagged_count"] == 2

    edit = admin_client.put(f"/lists/{watch['id']}", json={"name": "Watch", "items": ["1", "3"]})
    assert edit.status_code == 200

    after = admin_client.get(f"/queries/{query['id']}/poll").json()
    assert after["from_cache"] is False
    assert after["flags"]["flagged_count"] == 4
    assert after["data_hash"] != before["data_hash"]


def test_editing_a_list_leaves_unrelated_queries_cached(admin_client, sqlite_connection, query):
    other = make_query(admin_client, sqlite_connection["id"], "SELECT day FROM txns", name="other")
    watch = make_list(admin_client, items=["1"])
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    admin_client.post(f"/queries/{other['id']}/run")

    admin_client.put(f"/lists/{watch['id']}", json={"name": "Watch", "items": ["3"]})

    assert admin_client.get(f"/queries/{other['id']}/poll").json()["from_cache"] is True


def test_editing_a_list_invalidates_every_query_that_uses_it(admin_client, sqlite_connection):
    watch = make_list(admin_client, items=["1"])
    ids = []
    for n in range(2):
        q = make_query(admin_client, sqlite_connection["id"], "SELECT user_id FROM txns ORDER BY id", name=f"q{n}")
        put_rules(admin_client, q["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
        admin_client.post(f"/queries/{q['id']}/run")
        ids.append(q["id"])

    admin_client.put(f"/lists/{watch['id']}", json={"name": "Watch", "items": ["3"]})

    for query_id in ids:
        fresh = admin_client.get(f"/queries/{query_id}/poll").json()
        assert fresh["from_cache"] is False
        assert fresh["flags"]["flagged_count"] == 2


def test_an_edit_that_changes_nothing_but_the_items_still_moves_updated_at(admin_client):
    """The member-set cache keys on updated_at; an items-only edit must move it."""
    created = make_list(admin_client, items=["1"])
    edited = admin_client.put(f"/lists/{created['id']}", json={"name": "Watch", "items": ["2"]}).json()
    assert edited["updated_at"] > created["updated_at"]


def test_preview_evaluates_an_unsaved_list_rule(admin_client, sqlite_connection):
    watch = make_list(admin_client, items=["3"])
    response = admin_client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={
            "sql_text": "SELECT user_id, amount FROM txns ORDER BY id",
            "flag_rules": [list_rule("Trial", "user_id", "in_list", watch["id"])],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["flags"]["flagged_count"] == 2
    assert [r["index"] for r in body["flags"]["rows"]] == [3, 4]


def test_preview_with_an_unknown_list_is_refused(admin_client, sqlite_connection):
    response = admin_client.post(
        f"/connections/{sqlite_connection['id']}/query/preview",
        json={
            "sql_text": "SELECT user_id FROM txns",
            "flag_rules": [list_rule("Trial", "user_id", "in_list", "missing")],
        },
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "LIST_NOT_FOUND"


# --- Counts and deleting a list in use ---------------------------------------


def test_rule_count_counts_distinct_rules_not_conditions(admin_client, query):
    watch = make_list(admin_client)
    two_conditions = {
        "name": "Two",
        "severity": "low",
        "enabled": True,
        "conditions": [
            {"column_name": "user_id", "operator": "in_list", "list_id": watch["id"]},
            {"column_name": "comment", "operator": "not_in_list", "list_id": watch["id"]},
        ],
    }
    put_rules(admin_client, query["id"], [two_conditions, list_rule("Other", "user_id", "in_list", watch["id"])])

    assert admin_client.get(f"/lists/{watch['id']}").json()["rule_count"] == 2
    assert admin_client.get("/lists").json()[0]["rule_count"] == 2


def test_deleting_a_list_in_use_is_a_409_naming_the_rules(admin_client, sqlite_connection, query):
    watch = make_list(admin_client, "Watch")
    second_query = make_query(admin_client, sqlite_connection["id"], "SELECT user_id FROM txns", name="Second")
    put_rules(admin_client, query["id"], [list_rule("Rule A", "user_id", "in_list", watch["id"])])
    put_rules(admin_client, second_query["id"], [list_rule("Rule B", "user_id", "not_in_list", watch["id"])])

    response = admin_client.delete(f"/lists/{watch['id']}")

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "LIST_IN_USE"
    # Ordered by query name then rule name, case-insensitively: "q" before "Second".
    assert body["detail"]["rules"] == [
        {"rule_name": "Rule A", "query_id": query["id"], "query_name": query["name"]},
        {"rule_name": "Rule B", "query_id": second_query["id"], "query_name": "Second"},
    ]
    assert body["detail"]["hidden_rule_count"] == 0
    assert "Rule A" in body["message"] and "Rule B" in body["message"]
    assert admin_client.get(f"/lists/{watch['id']}").status_code == 200


def test_delete_succeeds_once_the_rule_is_removed(admin_client, query):
    watch = make_list(admin_client)
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    assert admin_client.delete(f"/lists/{watch['id']}").status_code == 409

    put_rules(admin_client, query["id"], [])

    assert admin_client.delete(f"/lists/{watch['id']}").status_code == 204


def test_a_disabled_rule_still_holds_its_list(admin_client, query):
    watch = make_list(admin_client)
    disabled = list_rule("Off", "user_id", "in_list", watch["id"])
    disabled["enabled"] = False
    put_rules(admin_client, query["id"], [disabled])
    assert admin_client.delete(f"/lists/{watch['id']}").status_code == 409


def test_the_database_refuses_to_delete_a_list_a_rule_reads(admin_client, query, session):
    """The RESTRICT foreign key is the backstop for a delete that races a rule
    save: it must fail even if the service's check is bypassed."""
    from sqlalchemy.exc import IntegrityError

    from app.features.lists.models import ItemList

    watch = make_list(admin_client)
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])

    session.delete(session.get(ItemList, watch["id"]))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_delete_that_loses_the_race_is_still_a_409(admin_client, query, monkeypatch):
    """The rule appears after the service's in-use check but before the delete."""
    from app.features.lists import service as list_service

    watch = make_list(admin_client)
    real = list_service.is_used
    calls = {"n": 0}

    def late(session, list_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # First look: nothing uses it. Then a rule save lands.
            result = real(session, list_id)
            put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", list_id)])
            return result
        return real(session, list_id)

    monkeypatch.setattr(list_service, "is_used", late)

    response = admin_client.delete(f"/lists/{watch['id']}")

    assert response.status_code == 409
    assert response.json()["error_code"] == "LIST_IN_USE"
    assert response.json()["detail"]["rules"][0]["rule_name"] == "R"


def test_deleting_a_query_frees_its_lists(admin_client, query):
    watch = make_list(admin_client)
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    assert admin_client.delete(f"/queries/{query['id']}").status_code in (200, 204)
    assert admin_client.delete(f"/lists/{watch['id']}").status_code == 204


def test_deleting_a_list_takes_its_items_with_it(admin_client, session):
    from sqlalchemy import func, select

    from app.features.lists.models import ListItem

    watch = make_list(admin_client, items=["a", "b", "c"])
    assert session.scalar(select(func.count()).select_from(ListItem)) == 3
    admin_client.delete(f"/lists/{watch['id']}")
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ListItem)) == 0


# --- Privacy of the in-use error ---------------------------------------------


def test_the_in_use_error_does_not_name_another_users_private_queries(
    client, admin_client, sqlite_connection, target_sqlite
):
    """Regression: DELETE 409 named every using rule and query, including a
    colleague's private ones. Only queries the caller can see are named."""
    make_user(email="alice@example.com", role=UserRole.ANALYST)
    make_user(email="bob@example.com", role=UserRole.ANALYST)
    alice = {"Authorization": "Bearer " + login(client, email="alice@example.com").json()["token"]}
    bob = {"Authorization": "Bearer " + login(client, email="bob@example.com").json()["token"]}

    def save_query(headers, name):
        response = client.post(
            f"/connections/{sqlite_connection['id']}/queries",
            json={"name": name, "sql_text": "SELECT user_id FROM txns"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    shared = client.post("/lists", json={"name": "Shared", "items": ["1"]}, headers=alice).json()
    alices_query = save_query(alice, "Alice query")
    bobs_query = save_query(bob, "BOB SECRET QUERY")
    for headers, query_id, rule_name in ((alice, alices_query, "Alice rule"), (bob, bobs_query, "BOB SECRET RULE")):
        saved = client.put(
            f"/queries/{query_id}/flag-rules",
            json={"rules": [list_rule(rule_name, "user_id", "in_list", shared["id"])]},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text

    response = client.delete(f"/lists/{shared['id']}", headers=alice)

    assert response.status_code == 409
    body = response.json()
    assert [r["rule_name"] for r in body["detail"]["rules"]] == ["Alice rule"]
    assert body["detail"]["hidden_rule_count"] == 1
    assert "BOB" not in response.text
    assert bobs_query not in response.text
    assert "1 rule on queries you cannot see" in body["message"]

    # An admin sees everything and hides nothing.
    admin_view = admin_client.delete(f"/lists/{shared['id']}").json()
    assert {r["rule_name"] for r in admin_view["detail"]["rules"]} == {"Alice rule", "BOB SECRET RULE"}
    assert admin_view["detail"]["hidden_rule_count"] == 0


def test_a_list_used_only_by_hidden_rules_is_still_protected(client, analyst_client, admin_client, query):
    shared = admin_client.post("/lists", json={"name": "Shared", "items": ["1"]}).json()
    put_rules(admin_client, query["id"], [list_rule("Admin rule", "user_id", "in_list", shared["id"])])
    # The analyst did not create the list, so this is 403 before the use check;
    # make the analyst the creator to reach it.
    mine = client.post("/lists", json={"name": "Mine", "items": ["1"]}, headers=analyst_client).json()
    put_rules(admin_client, query["id"], [list_rule("Admin rule", "user_id", "in_list", mine["id"])])

    response = client.delete(f"/lists/{mine['id']}", headers=analyst_client)

    assert response.status_code == 409
    assert response.json()["detail"]["rules"] == []
    assert response.json()["detail"]["hidden_rule_count"] == 1
    assert "Admin rule" not in response.text


# --- Hardening ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad\u0000name", "items": []},
        {"name": "ok", "items": ["bad\u0000item"]},
        {"name": "ok", "description": "bad\u0000", "items": []},
    ],
)
def test_nul_characters_are_refused(admin_client, body):
    """Postgres rejects NUL in text with a driver error; refuse it on every dialect."""
    response = admin_client.post("/lists", json=body)
    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_VALIDATION_ERROR"


def test_names_that_differ_only_by_non_ascii_case_collide(admin_client):
    """SQLite's lower() is ASCII-only; uniqueness is on a Python-computed key."""
    make_list(admin_client, "Éclair")
    response = admin_client.post("/lists", json={"name": "éclair", "items": []})
    assert response.status_code == 409
    assert response.json()["error_code"] == "LIST_NAME_TAKEN"


def test_names_that_differ_only_by_unicode_form_collide(admin_client):
    make_list(admin_client, "caf\u00e9")
    assert admin_client.post("/lists", json={"name": "cafe\u0301", "items": []}).status_code == 409


def test_the_name_key_follows_a_rename(admin_client):
    first = make_list(admin_client, "First")
    admin_client.put(f"/lists/{first['id']}", json={"name": "Renamed", "items": []})
    # The old name is free again, the new one is taken.
    assert admin_client.post("/lists", json={"name": "first", "items": []}).status_code == 201
    assert admin_client.post("/lists", json={"name": "RENAMED", "items": []}).status_code == 409


def test_a_same_tick_edit_cannot_serve_stale_members(admin_client, query, monkeypatch):
    """Regression: the member cache keyed on updated_at, so two edits that shared
    a timestamp served the first edit's members. It keys on an integer version."""
    from app.features.lists import service as list_service

    frozen = list_service.utcnow()
    monkeypatch.setattr(list_service, "utcnow", lambda: frozen)
    watch = make_list(admin_client, items=["1"])
    put_rules(admin_client, query["id"], [list_rule("R", "user_id", "in_list", watch["id"])])
    assert run_flagged(admin_client, query["id"]) == [0, 1]

    admin_client.put(f"/lists/{watch['id']}", json={"name": "Watch", "items": ["3"]})

    assert run_flagged(admin_client, query["id"]) == [3, 4]
    admin_client.put(f"/lists/{watch['id']}", json={"name": "Watch", "items": ["2"]})
    assert run_flagged(admin_client, query["id"]) == [2]


def test_boolean_cells_match_list_items_the_way_inline_in_does(admin_client, sqlite_connection):
    """`flagged` is 0/1 in the fixture (SQLite integer); an int cell is a number,
    so a list item `1` matches it and the word `yes` does not."""
    q = make_query(admin_client, sqlite_connection["id"], "SELECT flagged FROM txns ORDER BY id", name="b")
    ones = make_list(admin_client, "Ones", items=["1"])
    put_rules(admin_client, q["id"], [list_rule("R", "flagged", "in_list", ones["id"])])
    assert run_flagged(admin_client, q["id"]) == [1, 3]
    yes = make_list(admin_client, "Yes", items=["yes"])
    put_rules(admin_client, q["id"], [list_rule("R", "flagged", "in_list", yes["id"])])
    assert run_flagged(admin_client, q["id"]) == []


@pytest.mark.parametrize("order", [["1.0", "1"], ["1", "1.0"]])
def test_item_order_never_changes_what_a_list_matches(admin_client, sqlite_connection, order):
    """Regression: dedup on the numeric key alone kept whichever of "1.0" / "1"
    came first, and only "1" also matches a boolean true cell."""
    body = make_list(admin_client, "Order", order)
    assert sorted(body["items"]) == ["1", "1.0"]

    from app.features.lists.matching import is_member, members_from_items

    members = members_from_items(body["items"])
    assert is_member(members, True) is True
    assert is_member(members, 1) is True
    assert is_member(members, "1.00") is True


def test_true_duplicates_are_still_dropped(admin_client):
    body = make_list(admin_client, "Dups", ["2", "2.0", "2.00", "1", "1", "1.0"])
    assert body["items"] == ["2", "1", "1.0"]
    assert body["duplicates_dropped"] == 3


def test_the_default_item_limit_fits_the_default_request_cap(monkeypatch):
    """50,000 items of 20 characters is 1.2 MB and was refused with 413 before
    the item limit could answer. The default must fit under the request cap."""
    from app.config import get_settings

    monkeypatch.delenv("FAE_MAX_LIST_ITEMS", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.max_list_items == 20_000
    item = '"ACCT-000000000000000000000000000000000000"'  # 38 characters
    body = '{"name":"n","items":[' + ",".join([item] * settings.max_list_items) + "]}"
    assert len(body) < settings.max_request_bytes
