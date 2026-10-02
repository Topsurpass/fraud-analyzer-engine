"""Approval is bound to what was reviewed, and decisions cannot overtake each other.

Two defects an independent review found in the approval workflow.

**Bait and switch.** The author requests, the administrator opens the definition,
the author withdraws (which unfreezes the query), edits the SQL and requests again,
and the administrator approves from the page they already had open. Approval would
publish SQL nobody read. ``definition_fingerprint`` is shown with the definition and
sent back with the approval; a mismatch is ``DEFINITION_CHANGED``.

**Races.** Every transition is decided from a row read a moment earlier. These tests
force the other party's action into exactly that gap (the fingerprint computation and
the loading of the chart are the windows) and assert the stale action changes nothing.
"""

from __future__ import annotations

import pytest

from app.db.app_state import get_sessionmaker
from app.enums import AuditAction, UserRole
from app.errors import AppError, ErrorCode
from app.features.audit.models import AuditLog
from app.features.charts import service as chart_service
from app.features.charts.models import QueryChart
from app.features.users.models import User
from tests.test_chart_publishing import SQL, _auth
from tests.test_flag_rules_api import rule
from tests.test_lists_api import list_rule


@pytest.fixture
def people(client, app_db):
    return {
        "boss": _auth(client, "boss@example.com", UserRole.ADMIN),
        "alice": _auth(client, "alice@example.com", UserRole.ANALYST),
    }


def _definition(client, headers, chart_id):
    response = client.get(f"/queries/charts/{chart_id}/definition", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _fp(client, headers, chart_id):
    return _definition(client, headers, chart_id)["definition_fingerprint"]


def _approve(client, headers, chart_id, fingerprint):
    return client.post(
        f"/queries/charts/{chart_id}/publish/approve",
        headers=headers,
        json={"definition_fingerprint": fingerprint},
    )


def _state(client, headers, query_id, chart_id):
    charts = client.get(f"/queries/{query_id}/charts", headers=headers).json()["charts"]
    return next(c for c in charts if c["id"] == chart_id)


class Config:
    """One query with a 'Main' chart and an 'Other' one, every covered field settable."""

    def __init__(self, client, headers, connection_id, name="Cfg"):
        self.client, self.headers = client, headers
        self.query = client.post(
            f"/connections/{connection_id}/queries",
            headers=headers,
            json={"name": name, "sql_text": SQL, "row_limit": 100, "poll_interval_ms": 60_000},
        ).json()
        self.main = {"name": "Main", "chart_type": "bar", "x_field": "day", "y_field": "n",
                     "series_field": None, "surge_threshold_pct": 50}
        self.other = {"name": "Other", "chart_type": "table"}
        self.rules = [
            rule("Busy", "n", "gt", "3", severity="high"),
            rule("Quiet", "n", "lt", "1", severity="low"),
        ]
        self.apply()

    def apply(self, *, query=None):
        if query:
            response = self.client.put(f"/queries/{self.query['id']}", headers=self.headers, json=query)
            assert response.status_code == 200, response.text
        put = self.client.put(
            f"/queries/{self.query['id']}/charts", headers=self.headers,
            json={"charts": [self.main, self.other]},
        )
        assert put.status_code == 200, put.text
        self.charts = {c["name"]: c for c in put.json()["charts"]}
        rules = self.client.put(
            f"/queries/{self.query['id']}/flag-rules", headers=self.headers, json={"rules": self.rules}
        )
        assert rules.status_code == 200, rules.text

    @property
    def main_id(self):
        return self.charts["Main"]["id"]

    def fingerprint(self):
        return _fp(self.client, self.headers, self.main_id)


# --- the bait and switch -----------------------------------------------------


def _requested(client, people, sqlite_connection):
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    asked = client.post(f"/queries/charts/{cfg.main_id}/publish", headers=people["alice"])
    assert asked.status_code == 200, asked.text
    return cfg


def test_the_bait_and_switch_is_refused(client, people, sqlite_connection, session):
    cfg = _requested(client, people, sqlite_connection)
    reviewed = _fp(client, people["boss"], cfg.main_id)  # the admin opens the definition

    # The author withdraws, edits the SQL (the withdrawal unfroze it) and asks again.
    client.post(f"/queries/charts/{cfg.main_id}/publish/cancel", headers=people["alice"])
    cfg.apply(query={"sql_text": "SELECT 'something else entirely' AS day, 1 AS n"})
    client.post(f"/queries/charts/{cfg.main_id}/publish", headers=people["alice"])

    stale = _approve(client, people["boss"], cfg.main_id, reviewed)

    assert stale.status_code == 409, stale.text
    assert stale.json()["error_code"] == "DEFINITION_CHANGED"
    assert "changed" in stale.json()["message"]
    assert "review" in stale.json()["message"].lower()
    # Nothing was published, and the request is still waiting.
    state = _state(client, people["boss"], cfg.query["id"], cfg.main_id)
    assert state["publish_status"] == "pending"
    assert state["is_public"] is False
    session.expire_all()
    assert not [r for r in session.query(AuditLog).all() if r.action == AuditAction.CHART_PUBLISH_APPROVED]
    # The 409 does not hand the new value back: reviewing is the only way to learn it.
    assert "definition_fingerprint" not in stale.text and "detail\":null" in stale.text.replace(" ", "")

    current = client.get("/queries/charts/publish-requests", headers=people["boss"]).json()[0]
    assert current["definition_fingerprint"] != reviewed
    assert current["definition_fingerprint"] == _fp(client, people["boss"], cfg.main_id)
    assert _approve(client, people["boss"], cfg.main_id, current["definition_fingerprint"]).status_code == 200


def test_approving_what_was_read_works(client, people, sqlite_connection):
    cfg = _requested(client, people, sqlite_connection)
    shown = client.get("/queries/charts/publish-requests", headers=people["boss"]).json()[0]
    assert shown["definition_fingerprint"] == _fp(client, people["boss"], cfg.main_id)
    done = _approve(client, people["boss"], cfg.main_id, shown["definition_fingerprint"])
    assert done.status_code == 200, done.text
    assert done.json()["publish_status"] == "published"


def test_an_admin_edit_while_pending_also_voids_a_stale_approval(client, people, sqlite_connection):
    """An admin may edit a pending query; the page another admin has open is then stale too."""
    cfg = _requested(client, people, sqlite_connection)
    reviewed = _fp(client, people["boss"], cfg.main_id)
    edited = client.put(f"/queries/{cfg.query['id']}", headers=people["boss"], json={"sql_text": "SELECT 1 AS n"})
    assert edited.status_code == 200
    assert _approve(client, people["boss"], cfg.main_id, reviewed).status_code == 409


def test_the_fingerprint_is_required(client, people, sqlite_connection):
    cfg = _requested(client, people, sqlite_connection)
    url = f"/queries/charts/{cfg.main_id}/publish/approve"
    assert client.post(url, headers=people["boss"]).status_code == 422
    assert client.post(url, headers=people["boss"], json={}).status_code == 422
    assert client.post(url, headers=people["boss"], json={"definition_fingerprint": ""}).status_code == 422
    assert _state(client, people["boss"], cfg.query["id"], cfg.main_id)["publish_status"] == "pending"


def test_reject_needs_no_fingerprint(client, people, sqlite_connection):
    cfg = _requested(client, people, sqlite_connection)
    done = client.post(f"/queries/charts/{cfg.main_id}/publish/reject", headers=people["boss"])
    assert done.status_code == 200
    assert done.json()["publish_status"] == "private"


def test_not_pending_wins_over_a_wrong_fingerprint(client, people, sqlite_connection):
    cfg = Config(client, people["alice"], sqlite_connection["id"])  # never requested
    response = _approve(client, people["boss"], cfg.main_id, "0" * 64)
    assert response.status_code == 409
    assert response.json()["error_code"] == "PUBLISH_NOT_PENDING"


def test_a_wrong_fingerprint_on_a_pending_chart_is_a_definition_conflict(client, people, sqlite_connection):
    cfg = _requested(client, people, sqlite_connection)
    response = _approve(client, people["boss"], cfg.main_id, "0" * 64)
    assert response.status_code == 409
    assert response.json()["error_code"] == "DEFINITION_CHANGED"


def test_an_analyst_still_cannot_approve_with_the_right_fingerprint(client, people, sqlite_connection):
    cfg = _requested(client, people, sqlite_connection)
    right = _fp(client, people["alice"], cfg.main_id)
    assert _approve(client, people["alice"], cfg.main_id, right).status_code == 403


# --- what the fingerprint covers ---------------------------------------------


def _mutations():
    def set_main(**kw):
        def apply(cfg):
            cfg.main = {**cfg.main, **kw}
        return apply

    def set_rule(index, **kw):
        def apply(cfg):
            first = cfg.rules[index]
            condition = {**first["conditions"][0], **{k: v for k, v in kw.items() if k in
                         ("column_name", "operator", "value", "value2")}}
            cfg.rules[index] = {**first, **{k: v for k, v in kw.items() if k in ("name", "severity", "enabled")},
                                "conditions": [condition]}
        return apply

    return {
        "sql_text": lambda cfg: ("query", {"sql_text": SQL + " "}),
        "row_limit": lambda cfg: ("query", {"row_limit": 101}),
        "poll_interval_ms": lambda cfg: ("query", {"poll_interval_ms": 120_000}),
        "chart_type": set_main(chart_type="line"),
        "x_field": set_main(x_field="n"),
        "y_field": set_main(y_field="day"),
        "series_field": set_main(series_field="day"),
        "surge_threshold_pct": set_main(surge_threshold_pct=75),
        "rule name": set_rule(0, name="Busy!"),
        "rule severity": set_rule(0, severity="medium"),
        "rule enabled": set_rule(0, enabled=False),
        "condition column": set_rule(0, column_name="day"),
        "condition operator": set_rule(0, operator="gte"),
        "condition value": set_rule(0, value="4"),
        "condition value2": set_rule(1, operator="between", value="0", value2="9"),
        "rule order": lambda cfg: cfg.rules.reverse(),
        "a rule removed": lambda cfg: cfg.rules.pop(),
        "a rule added": lambda cfg: cfg.rules.append(rule("Third", "n", "eq", "7")),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_changing_any_covered_field_changes_the_fingerprint(client, people, sqlite_connection, name):
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    before = cfg.fingerprint()
    result = _mutations()[name](cfg)
    cfg.apply(query=result[1] if isinstance(result, tuple) else None)
    assert cfg.fingerprint() != before, name


def test_a_changed_list_id_changes_the_fingerprint(client, people, sqlite_connection):
    one = client.post("/lists", headers=people["boss"], json={"name": "One", "items": ["1"]}).json()
    two = client.post("/lists", headers=people["boss"], json={"name": "Two", "items": ["1"]}).json()
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    cfg.rules = [list_rule("Listed", "day", "in_list", one["id"])]
    cfg.apply()
    before = cfg.fingerprint()
    cfg.rules = [list_rule("Listed", "day", "in_list", two["id"])]
    cfg.apply()
    assert cfg.fingerprint() != before


def test_nothing_else_changes_it(client, people, sqlite_connection):
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    before = cfg.fingerprint()

    # Saving the very same definition again (new rule ids, same content).
    cfg.apply()
    assert cfg.fingerprint() == before
    # The query's own name and description.
    renamed = client.put(
        f"/queries/{cfg.query['id']}", headers=people["alice"],
        json={"name": "Renamed", "description": "Now described"},
    )
    assert renamed.status_code == 200, renamed.text
    assert cfg.fingerprint() == before
    # A different chart on the same query, edited in every way.
    cfg.other = {"name": "Other", "chart_type": "pie", "x_field": "day", "y_field": "n", "surge_threshold_pct": 5}
    cfg.apply()
    assert cfg.fingerprint() == before


def test_the_other_chart_has_its_own_fingerprint(client, people, sqlite_connection):
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    assert _fp(client, people["alice"], cfg.charts["Other"]["id"]) != cfg.fingerprint()


def test_the_same_definition_hashes_the_same_wherever_it_lives(client, people, sqlite_connection):
    first = Config(client, people["alice"], sqlite_connection["id"], name="First")
    second = Config(client, people["alice"], sqlite_connection["id"], name="Second")
    assert first.fingerprint() == second.fingerprint()


def test_the_fingerprint_is_a_sha256_hex_digest(client, people, sqlite_connection):
    value = Config(client, people["alice"], sqlite_connection["id"]).fingerprint()
    assert len(value) == 64 and set(value) <= set("0123456789abcdef")


def test_the_definition_and_the_queue_agree_in_every_state(client, people, sqlite_connection):
    cfg = _requested(client, people, sqlite_connection)
    queue = client.get("/queries/charts/publish-requests", headers=people["boss"]).json()[0]
    for who in ("alice", "boss"):
        assert _fp(client, people[who], cfg.main_id) == queue["definition_fingerprint"], who


def test_a_lists_items_are_deliberately_not_part_of_it(client, people, sqlite_connection):
    """The documented limit: a list can change what a published rule flags without
    changing the definition. If this ever fails, the limit has been closed and the
    docs should say so."""
    lst = client.post("/lists", headers=people["boss"], json={"name": "Watch", "items": ["1"]}).json()
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    cfg.rules = [list_rule("Listed", "day", "in_list", lst["id"])]
    cfg.apply()
    before = cfg.fingerprint()
    edited = client.put(
        f"/lists/{lst['id']}", headers=people["boss"],
        json={"name": "Watch", "description": None, "items": ["1", "2", "3"]},
    )
    assert edited.status_code == 200, edited.text
    assert cfg.fingerprint() == before


# --- races -------------------------------------------------------------------


def _user(email):
    with get_sessionmaker()() as s:
        found = s.query(User).filter(User.email == email).one()
        s.expunge(found)
        return found


def _in_other_session(fn):
    with get_sessionmaker()() as other:
        fn(other)


def _once(real, action):
    """``real``, plus ``action()`` once, right after it returns.

    The action is another party acting in the window between one of the service's
    reads and its write, and it calls the same service functions, so the wrapper
    disarms itself rather than recursing.
    """
    state = {"armed": True}

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        if state["armed"]:
            state["armed"] = False
            action()
        return result

    return wrapper


def _row(chart_id):
    with get_sessionmaker()() as s:
        chart = s.get(QueryChart, chart_id)
        return {
            "is_public": chart.is_public,
            "status": chart.publish_status,
            "published_by": chart.published_by,
            "requested_at": chart.publish_requested_at,
            "rejection": chart.publish_rejection,
        }


def _audits(session, action):
    session.expire_all()
    return [r for r in session.query(AuditLog).all() if r.action == action]


def test_a_withdrawal_between_the_check_and_the_write_stops_the_approval(
    client, people, sqlite_connection, monkeypatch, session
):
    cfg = _requested(client, people, sqlite_connection)
    boss, alice = _user("boss@example.com"), _user("alice@example.com")
    reviewed = _fp(client, people["boss"], cfg.main_id)
    real = chart_service.fingerprint_of

    def withdrawn_meanwhile(s, chart):
        value = real(s, chart)
        _in_other_session(lambda o: chart_service.cancel_request(o, cfg.main_id, alice))
        return value

    monkeypatch.setattr(chart_service, "fingerprint_of", withdrawn_meanwhile)
    with get_sessionmaker()() as mine:
        with pytest.raises(AppError) as refused:
            chart_service.approve(mine, cfg.main_id, mine.merge(boss), reviewed)

    assert refused.value.error_code == ErrorCode.PUBLISH_NOT_PENDING
    assert _row(cfg.main_id)["is_public"] is False
    assert _row(cfg.main_id)["status"] == "private"
    assert _audits(session, AuditAction.CHART_PUBLISH_APPROVED) == []


def test_a_withdraw_and_re_ask_between_the_check_and_the_write_also_stops_it(
    client, people, sqlite_connection, monkeypatch
):
    """The replacement request is a different request: approving it needs its own review."""
    cfg = _requested(client, people, sqlite_connection)
    boss, alice = _user("boss@example.com"), _user("alice@example.com")
    reviewed = _fp(client, people["boss"], cfg.main_id)
    real = chart_service.fingerprint_of

    def replaced_meanwhile(s, chart):
        value = real(s, chart)

        def swap(o):
            chart_service.cancel_request(o, cfg.main_id, alice)
            chart_service.publish(o, cfg.main_id, alice)

        _in_other_session(swap)
        return value

    monkeypatch.setattr(chart_service, "fingerprint_of", replaced_meanwhile)
    with get_sessionmaker()() as mine:
        with pytest.raises(AppError) as refused:
            chart_service.approve(mine, cfg.main_id, mine.merge(boss), reviewed)

    assert refused.value.error_code == ErrorCode.PUBLISH_NOT_PENDING
    assert _row(cfg.main_id)["status"] == "pending"


def test_a_withdrawal_between_the_check_and_the_write_stops_a_rejection(
    client, people, sqlite_connection, monkeypatch, session
):
    cfg = _requested(client, people, sqlite_connection)
    boss, alice = _user("boss@example.com"), _user("alice@example.com")
    real = chart_service._pending_chart

    def withdrawn_meanwhile(s, chart_id):
        chart = real(s, chart_id)
        _in_other_session(lambda o: chart_service.cancel_request(o, cfg.main_id, alice))
        return chart

    monkeypatch.setattr(chart_service, "_pending_chart", withdrawn_meanwhile)
    with get_sessionmaker()() as mine:
        with pytest.raises(AppError) as refused:
            chart_service.reject(mine, cfg.main_id, mine.merge(boss), "too late")

    assert refused.value.error_code == ErrorCode.PUBLISH_NOT_PENDING
    state = _row(cfg.main_id)
    assert state["rejection"] is None and state["status"] == "private"
    assert _audits(session, AuditAction.CHART_PUBLISH_REJECTED) == []


def test_a_stale_withdrawal_cannot_clear_a_publication(
    client, people, sqlite_connection, monkeypatch, session
):
    cfg = _requested(client, people, sqlite_connection)
    boss, alice = _user("boss@example.com"), _user("alice@example.com")
    fp = _fp(client, people["boss"], cfg.main_id)
    # The author's cancel has read "pending"; then the administrator approves.
    monkeypatch.setattr(
        chart_service, "_chart_for_owner",
        _once(
            chart_service._chart_for_owner,
            lambda: _in_other_session(
                lambda o: chart_service.approve(o, cfg.main_id, o.merge(boss), fp)
            ),
        ),
    )
    with get_sessionmaker()() as mine:
        result = chart_service.cancel_request(mine, cfg.main_id, mine.merge(alice))
        assert result.publish_status == "published"

    state = _row(cfg.main_id)
    assert state["is_public"] is True
    assert state["published_by"] == alice.id
    assert _audits(session, AuditAction.CHART_PUBLISH_CANCELLED) == []


def test_a_stale_request_cannot_downgrade_a_publication(
    client, people, sqlite_connection, monkeypatch, session
):
    cfg = Config(client, people["alice"], sqlite_connection["id"])
    boss, alice = _user("boss@example.com"), _user("alice@example.com")
    # Alice's request has read "private"; then an administrator publishes.
    monkeypatch.setattr(
        chart_service, "_chart_for_owner",
        _once(
            chart_service._chart_for_owner,
            lambda: _in_other_session(
                lambda o: chart_service.publish(o, cfg.main_id, o.merge(boss))
            ),
        ),
    )
    with get_sessionmaker()() as mine:
        chart_service.publish(mine, cfg.main_id, mine.merge(alice))

    state = _row(cfg.main_id)
    assert state["is_public"] is True and state["requested_at"] is None
    assert state["published_by"] == boss.id
    assert _audits(session, AuditAction.CHART_PUBLISH_REQUESTED) == []


def test_a_stale_unpublish_by_the_author_cannot_retract_an_admins_publication(
    client, people, sqlite_connection, monkeypatch
):
    """The author may only retract what they published themselves; that must hold
    even if the publisher changed between reading the chart and writing."""
    cfg = _requested(client, people, sqlite_connection)
    boss, alice = _user("boss@example.com"), _user("alice@example.com")
    fp = _fp(client, people["boss"], cfg.main_id)
    # Approval makes alice the publisher.
    client.post(
        f"/queries/charts/{cfg.main_id}/publish/approve", headers=people["boss"],
        json={"definition_fingerprint": fp},
    )
    def swap(o):
        chart_service.unpublish(o, cfg.main_id, o.merge(boss))
        chart_service.publish(o, cfg.main_id, o.merge(boss))  # now the admin's

    # Alice has read herself as the publisher; then the administrator takes it over.
    monkeypatch.setattr(
        chart_service, "_chart_for_owner",
        _once(chart_service._chart_for_owner, lambda: _in_other_session(swap)),
    )
    with get_sessionmaker()() as mine:
        chart_service.unpublish(mine, cfg.main_id, mine.merge(alice))

    state = _row(cfg.main_id)
    assert state["is_public"] is True
    assert state["published_by"] == boss.id
