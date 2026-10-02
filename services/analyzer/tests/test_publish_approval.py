"""Publishing by request: an analyst asks, an administrator decides.

Every test here is a consequence of one rule: an analyst's publish never makes a
chart visible to anyone by itself. The older behaviour (publish takes effect at
once) is covered, for administrators, in ``test_chart_publishing.py``.
"""

from __future__ import annotations

import pytest

from app.enums import AuditAction, UserRole
from app.features.audit.models import AuditLog
from tests.test_chart_publishing import SQL, _auth, _query_with_chart
from tests.test_flag_rules_api import rule


@pytest.fixture
def people(client, app_db):
    return {
        "boss": _auth(client, "boss@example.com", UserRole.ADMIN),
        "alice": _auth(client, "alice@example.com", UserRole.ANALYST),
        "bob": _auth(client, "bob@example.com", UserRole.ANALYST),
    }


@pytest.fixture
def pending(client, people, sqlite_connection):
    """Alice's chart, requested and waiting."""
    query, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    asked = client.post(f"/queries/charts/{chart['id']}/publish", headers=people["alice"])
    assert asked.status_code == 200, asked.text
    return query, chart


def _post(client, headers, path, **kw):
    return client.post(path, headers=headers, **kw)


def _approve(client, headers, chart_id, fingerprint=None):
    """Approve as the dashboard does: send back the fingerprint of what was read.

    ``fingerprint`` overrides it, for the callers who are meant to be refused (they
    cannot read the definition of somebody else's pending chart anyway).
    """
    if fingerprint is None:
        fingerprint = client.get(
            f"/queries/charts/{chart_id}/definition", headers=headers
        ).json()["definition_fingerprint"]
    return client.post(
        f"/queries/charts/{chart_id}/publish/approve",
        headers=headers,
        json={"definition_fingerprint": fingerprint},
    )


def _status(client, headers, chart_id):
    published = client.get("/queries/charts/published", headers=headers).json()
    return chart_id in [c["id"] for c in published]


# --- asking ----------------------------------------------------------------


def test_an_analysts_publish_is_a_request_not_a_publication(client, people, pending):
    _, chart = pending
    body = client.get(f"/queries/{chart['query_id']}/charts", headers=people["alice"]).json()
    mine = body["charts"][0]
    assert mine["publish_status"] == "pending"
    assert mine["is_public"] is False
    assert mine["published_at"] is None
    assert mine["publish_requested_at"] is not None
    # And nobody else can see it.
    assert not _status(client, people["bob"], chart["id"])
    assert client.get(f"/queries/charts/{chart['id']}/poll", headers=people["bob"]).status_code == 404


def test_an_admin_publishes_at_once(client, people, sqlite_connection):
    _, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    published = _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish").json()
    assert published["publish_status"] == "published"
    assert published["is_public"] is True
    assert _status(client, people["bob"], chart["id"])


def test_asking_twice_keeps_the_first_request(client, people, pending):
    _, chart = pending
    first = client.get(f"/queries/{chart['query_id']}/charts", headers=people["alice"]).json()["charts"][0]
    again = _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish").json()
    assert again["publish_status"] == "pending"
    assert again["publish_requested_at"] == first["publish_requested_at"]


def test_a_chart_in_the_charts_list_carries_its_status(client, people, sqlite_connection):
    _, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    assert chart["publish_status"] == "private"
    assert chart["publish_requested_at"] is None
    assert chart["publish_rejection"] is None


def test_someone_elses_chart_cannot_be_requested(client, people, sqlite_connection):
    _, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    # 404, not 403: confirming it exists tells bob what alice is doing.
    assert _post(client, people["bob"], f"/queries/charts/{chart['id']}/publish").status_code == 404


# --- deciding --------------------------------------------------------------


def test_an_admin_approves_and_everyone_can_then_see_it(client, people, pending):
    _, chart = pending
    approved = _approve(client, people["boss"], chart['id'])
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["publish_status"] == "published"
    assert body["is_public"] is True
    assert body["publish_requested_at"] is None
    assert _status(client, people["bob"], chart["id"])
    assert client.get(
        f"/queries/charts/{chart['id']}/poll?force=true", headers=people["bob"]
    ).status_code == 200


def test_an_approval_keeps_the_author_as_publisher(client, people, pending):
    """So the author can retract their own publication, as before."""
    _, chart = pending
    body = _approve(client, people["boss"], chart['id']).json()
    alice_id = client.get("/auth/me", headers=people["alice"]).json()["id"]
    assert body["published_by"] == alice_id
    assert body["published_by_name"] == "An Analyst"
    retracted = _post(client, people["alice"], f"/queries/charts/{chart['id']}/unpublish")
    assert retracted.status_code == 200
    assert retracted.json()["publish_status"] == "private"


def test_only_an_administrator_can_approve_or_reject(client, people, pending):
    _, chart = pending
    for who in ("alice", "bob"):
        approve = _approve(client, people[who], chart["id"], fingerprint="x")
        reject = _post(client, people[who], f"/queries/charts/{chart['id']}/publish/reject", json={})
        assert approve.status_code == 403, who
        assert reject.status_code == 403, who
    assert not _status(client, people["bob"], chart["id"])
    still = client.get(f"/queries/{chart['query_id']}/charts", headers=people["alice"]).json()["charts"][0]
    assert still["publish_status"] == "pending"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_deciding_on_a_chart_nobody_asked_about_is_a_conflict(client, people, sqlite_connection, action):
    _, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    body = {"definition_fingerprint": "x"} if action == "approve" else {}
    response = _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/{action}", json=body)
    assert response.status_code == 409
    assert response.json()["error_code"] == "PUBLISH_NOT_PENDING"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_request_can_only_be_decided_once(client, people, pending, action):
    _, chart = pending
    assert _approve(client, people["boss"], chart['id']).status_code == 200
    body = {"definition_fingerprint": "x"} if action == "approve" else {}
    again = _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/{action}", json=body)
    assert again.status_code == 409
    assert again.json()["error_code"] == "PUBLISH_NOT_PENDING"


def test_a_rejection_keeps_the_chart_private_and_says_why(client, people, pending):
    _, chart = pending
    rejected = _post(
        client, people["boss"], f"/queries/charts/{chart['id']}/publish/reject",
        json={"reason": "Uses a column we do not share."},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["publish_status"] == "private"
    assert body["is_public"] is False
    assert body["publish_requested_at"] is None
    assert body["publish_rejection"]["reason"] == "Uses a column we do not share."
    assert body["publish_rejection"]["rejected_by_name"] == "An Analyst"  # make_user's default name
    assert body["publish_rejection"]["rejected_at"].endswith("Z") or "+" in body["publish_rejection"]["rejected_at"]
    assert not _status(client, people["bob"], chart["id"])


def test_a_rejection_needs_no_reason_and_no_body(client, people, pending):
    _, chart = pending
    body = _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/reject").json()
    assert body["publish_rejection"]["reason"] is None


def test_a_long_reason_is_refused(client, people, pending):
    _, chart = pending
    response = _post(
        client, people["boss"], f"/queries/charts/{chart['id']}/publish/reject",
        json={"reason": "x" * 501},
    )
    assert response.status_code == 422


def test_asking_again_clears_the_rejection(client, people, pending):
    _, chart = pending
    _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/reject", json={"reason": "No."})
    again = _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish").json()
    assert again["publish_status"] == "pending"
    assert again["publish_rejection"] is None


def test_an_admin_publishing_a_pending_chart_directly_clears_the_request(client, people, pending):
    _, chart = pending
    body = _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish").json()
    assert body["publish_status"] == "published"
    assert body["publish_requested_at"] is None
    # An administrator's own publication is theirs to retract, not the author's.
    assert _post(client, people["alice"], f"/queries/charts/{chart['id']}/unpublish").status_code == 403


# --- withdrawing -----------------------------------------------------------


def test_the_author_can_withdraw_a_request(client, people, pending):
    _, chart = pending
    body = _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish/cancel").json()
    assert body["publish_status"] == "private"
    assert body["publish_requested_at"] is None
    # With nothing left to decide on, the admin cannot approve it any more.
    assert _approve(client, people["boss"], chart['id']).status_code == 409


def test_withdrawing_also_clears_a_rejection_notice(client, people, pending):
    _, chart = pending
    _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/reject", json={"reason": "No."})
    body = _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish/cancel").json()
    assert body["publish_rejection"] is None
    assert body["publish_status"] == "private"


def test_withdrawing_does_not_unpublish(client, people, pending):
    _, chart = pending
    _approve(client, people["boss"], chart['id'])
    body = _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish/cancel").json()
    assert body["publish_status"] == "published"


def test_only_the_author_or_an_admin_can_withdraw(client, people, pending):
    _, chart = pending
    assert _post(client, people["bob"], f"/queries/charts/{chart['id']}/publish/cancel").status_code == 404
    assert _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/cancel").status_code == 200


# --- the admin's queue -----------------------------------------------------


def test_the_queue_lists_pending_requests_for_an_admin(client, people, pending, sqlite_connection):
    query, chart = pending
    rows = client.get("/queries/charts/publish-requests", headers=people["boss"])
    assert rows.status_code == 200, rows.text
    [item] = rows.json()
    assert item["chart"]["id"] == chart["id"]
    assert item["chart"]["publish_status"] == "pending"
    assert item["query_id"] == query["id"]
    assert item["query_name"] == query["name"]
    assert item["connection_id"] == sqlite_connection["id"]
    assert item["connection_name"] == sqlite_connection["name"]
    assert item["requested_by"]["email"] == "alice@example.com"
    assert item["requested_by"]["full_name"] == "An Analyst"
    assert item["requested_by"]["id"]
    assert item["requested_at"] == item["chart"]["publish_requested_at"]


def test_the_queue_is_for_administrators_only(client, people, pending):
    for who in ("alice", "bob"):
        response = client.get("/queries/charts/publish-requests", headers=people[who])
        assert response.status_code == 403, who
        assert response.json()["error_code"] == "FORBIDDEN"


def test_the_queue_holds_only_what_is_waiting_oldest_first(client, people, sqlite_connection):
    ids = []
    for name in ("First", "Second", "Third", "Fourth"):
        _, chart = _query_with_chart(client, people["alice"], sqlite_connection, name=name)
        ids.append(chart["id"])
    for chart_id in ids[:3]:
        _post(client, people["alice"], f"/queries/charts/{chart_id}/publish")
    _approve(client, people["boss"], ids[1])  # decided
    # ids[3] was never requested.

    queue = client.get("/queries/charts/publish-requests", headers=people["boss"]).json()

    assert [r["chart"]["id"] for r in queue] == [ids[0], ids[2]]


def test_the_queue_is_empty_when_nothing_is_waiting(client, people, app_db):
    assert client.get("/queries/charts/publish-requests", headers=people["boss"]).json() == []


# --- the freeze ------------------------------------------------------------


def test_a_pending_request_freezes_the_query_for_its_author(client, people, pending):
    query, chart = pending
    update = client.put(f"/queries/{query['id']}", headers=people["alice"], json={"sql_text": "SELECT 1 AS n"})
    assert update.status_code == 409
    assert update.json()["error_code"] == "QUERY_FROZEN"
    # The refusal names the way out, like the published one does.
    assert "Withdraw" in update.json()["message"]
    assert chart["name"] in update.json()["message"]


def test_a_pending_request_freezes_the_charts_delete_and_rules_too(client, people, pending):
    query, _ = pending
    assert client.delete(f"/queries/{query['id']}", headers=people["alice"]).status_code == 409
    assert client.put(
        f"/queries/{query['id']}/charts", headers=people["alice"],
        json={"charts": [{"name": "Rewired", "chart_type": "table"}]},
    ).status_code == 409
    changed = client.put(
        f"/queries/{query['id']}/flag-rules", headers=people["alice"],
        json={"rules": [rule("Sneaky", "amount", "gt", "1")]},
    )
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "QUERY_FROZEN"


def test_withdrawing_unfreezes_the_query(client, people, pending):
    query, chart = pending
    _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish/cancel")
    assert client.put(
        f"/queries/{query['id']}", headers=people["alice"], json={"sql_text": "SELECT 1 AS n"}
    ).status_code == 200


def test_a_rejection_unfreezes_the_query(client, people, pending):
    query, chart = pending
    _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish/reject", json={})
    assert client.put(
        f"/queries/{query['id']}", headers=people["alice"], json={"sql_text": "SELECT 1 AS n"}
    ).status_code == 200


def test_an_admin_can_edit_a_pending_query_without_changing_its_status(client, people, pending):
    query, chart = pending
    edited = client.put(f"/queries/{query['id']}", headers=people["boss"], json={"sql_text": "SELECT 1 AS n"})
    assert edited.status_code == 200, edited.text
    now = client.get(f"/queries/{query['id']}/charts", headers=people["boss"]).json()["charts"][0]
    assert now["publish_status"] == "pending"


def test_a_published_query_now_freezes_its_rules_too(client, people, sqlite_connection):
    """Rules decide what viewers are alerted to, so they are as fixed as the SQL."""
    query, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    _post(client, people["boss"], f"/queries/charts/{chart['id']}/publish")
    changed = client.put(
        f"/queries/{query['id']}/flag-rules", headers=people["alice"],
        json={"rules": [rule("Late", "amount", "gt", "1")]},
    )
    assert changed.status_code == 409
    # An administrator is the approving authority and may still edit.
    assert client.put(
        f"/queries/{query['id']}/flag-rules", headers=people["boss"],
        json={"rules": [rule("Late", "amount", "gt", "1")]},
    ).status_code == 200


# --- the record ------------------------------------------------------------


def _audit(session, action):
    session.expire_all()
    return [row for row in session.query(AuditLog).all() if row.action == action]


def test_every_transition_is_audited_with_its_actor(client, people, sqlite_connection, session):
    _, a = _query_with_chart(client, people["alice"], sqlite_connection, name="A")
    _, b = _query_with_chart(client, people["alice"], sqlite_connection, name="B")
    _, c = _query_with_chart(client, people["alice"], sqlite_connection, name="C")
    for chart in (a, b, c):
        _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish")
    _approve(client, people["boss"], a['id'])
    _post(client, people["boss"], f"/queries/charts/{b['id']}/publish/reject", json={"reason": "No."})
    _post(client, people["alice"], f"/queries/charts/{c['id']}/publish/cancel")
    _post(client, people["alice"], f"/queries/charts/{a['id']}/unpublish")
    _, d = _query_with_chart(client, people["alice"], sqlite_connection, name="D")
    _post(client, people["boss"], f"/queries/charts/{d['id']}/publish")

    alice = client.get("/auth/me", headers=people["alice"]).json()["id"]
    boss = client.get("/auth/me", headers=people["boss"]).json()["id"]

    def one(action, target):
        rows = [r for r in _audit(session, action) if r.target_id == target]
        assert len(rows) == 1, (action, target, rows)
        return rows[0]

    assert len(_audit(session, AuditAction.CHART_PUBLISH_REQUESTED)) == 3
    assert one(AuditAction.CHART_PUBLISH_REQUESTED, a["id"]).actor_id == alice
    approved = one(AuditAction.CHART_PUBLISH_APPROVED, a["id"])
    assert approved.actor_id == boss
    assert approved.target_type == "chart"
    assert approved.detail["requested_by"] == alice
    rejected = one(AuditAction.CHART_PUBLISH_REJECTED, b["id"])
    assert rejected.actor_id == boss
    assert rejected.detail["reason"] == "No."
    assert one(AuditAction.CHART_PUBLISH_CANCELLED, c["id"]).actor_id == alice
    assert one(AuditAction.CHART_UNPUBLISHED, a["id"]).actor_id == alice
    assert one(AuditAction.CHART_PUBLISHED, d["id"]).actor_id == boss


def test_a_refused_decision_leaves_no_audit_entry(client, people, pending, session):
    _, chart = pending
    _approve(client, people["bob"], chart['id'], fingerprint="x")
    assert _audit(session, AuditAction.CHART_PUBLISH_APPROVED) == []


def test_the_audit_log_endpoint_lists_them(client, people, pending):
    _, chart = pending
    _approve(client, people["boss"], chart['id'])
    log = client.get("/audit-log", headers=people["boss"])
    assert log.status_code == 200, log.text
    text = log.text
    assert "chart_publish_requested" in text
    assert "chart_publish_approved" in text


def test_requesting_does_not_cost_an_execution(client, people, sqlite_connection):
    query, chart = _query_with_chart(client, people["alice"], sqlite_connection)
    client.get(f"/queries/{query['id']}/poll", headers=people["alice"])
    before = len(client.get(f"/queries/{query['id']}/logs", headers=people["alice"]).json())

    _post(client, people["alice"], f"/queries/charts/{chart['id']}/publish")
    _approve(client, people["boss"], chart['id'])
    client.get(f"/queries/{query['id']}/poll", headers=people["alice"])
    _post(client, people["alice"], f"/queries/charts/{chart['id']}/unpublish")

    assert len(client.get(f"/queries/{query['id']}/logs", headers=people["alice"]).json()) == before
    assert SQL  # keep the import honest
