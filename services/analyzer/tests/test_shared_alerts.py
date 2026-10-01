"""Alerts from a published chart reach everyone it was shared with.

Findings are stored once per query. What makes sharing them safe is that a
dismissal belongs to a person: a viewer clearing what they have looked at must
not hide anything from the author, an administrator or the next viewer. The
target fixture's ``txns`` has amounts 10.5, 900.0, 12.0, 750.25, 20.0, so the
rule ``amount > 500`` flags exactly two rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.enums import UserRole
from app.features.flag_rules.models import FlaggedRow, FlagDismissal
from tests.test_auth_api import login, make_user
from tests.test_chart_publishing import _approved, _auth
from tests.test_flag_rules_api import rule

SQL = "SELECT day, amount, comment FROM txns"


@pytest.fixture
def people(client, app_db):
    return {
        "boss": _auth(client, "boss@example.com", UserRole.ADMIN),
        "alice": _auth(client, "alice@example.com", UserRole.ANALYST),
        "bob": _auth(client, "bob@example.com", UserRole.ANALYST),
        "carol": _auth(client, "carol@example.com", UserRole.ANALYST),
    }


@pytest.fixture
def alices(client, people, sqlite_connection):
    """Alice's flagging query, already run so its two findings are stored."""
    query = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        headers=people["alice"],
        json={"name": "Big ones", "sql_text": SQL},
    ).json()
    put = client.put(
        f"/queries/{query['id']}/flag-rules",
        headers=people["alice"],
        json={"rules": [rule("Large", "amount", "gt", "500")]},
    )
    assert put.status_code == 200, put.text
    chart = client.put(
        f"/queries/{query['id']}/charts",
        headers=people["alice"],
        json={"charts": [{"name": "Big chart", "chart_type": "table"}]},
    ).json()["charts"][0]
    assert client.post(f"/queries/{query['id']}/run", headers=people["alice"]).status_code == 200
    return query, chart


def _summary(client, headers):
    return client.get("/flagged/summary", headers=headers).json()


def _section(client, headers, connection_id, query_id):
    body = client.get(f"/connections/{connection_id}/flagged", headers=headers).json()
    return next((s for s in body["queries"] if s["query_id"] == query_id), None)


def _count(client, headers, query_id):
    entry = next((q for q in _summary(client, headers)["queries"] if q["query_id"] == query_id), None)
    return entry["flagged_count"] if entry else 0


def _poll(client, headers, chart_id):
    return client.get(f"/queries/charts/{chart_id}/poll?force=true", headers=headers).json()


def _rows(client, headers, connection_id, query_id):
    section = _section(client, headers, connection_id, query_id)
    return [row["fingerprint"] for row in section["rows"]] if section else []


# --- who sees what ---------------------------------------------------------


def test_a_viewer_hears_nothing_of_an_unpublished_query(client, people, alices, sqlite_connection):
    query, _ = alices
    assert _count(client, people["bob"], query["id"]) == 0
    assert _summary(client, people["bob"])["flagged_count"] == 0
    assert _section(client, people["bob"], sqlite_connection["id"], query["id"]) is None


def test_publishing_extends_the_alerts_to_everyone(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])

    for who in ("bob", "carol"):
        assert _count(client, people[who], query["id"]) == 2, who
        assert _summary(client, people[who])["flagged_count"] == 2, who
        section = _section(client, people[who], sqlite_connection["id"], query["id"])
        assert section["flagged_count"] == 2
        assert len(section["rows"]) == 2


def test_a_pending_chart_shares_nothing_yet(client, people, alices):
    query, chart = alices
    client.post(f"/queries/charts/{chart['id']}/publish", headers=people["alice"])
    assert _count(client, people["bob"], query["id"]) == 0


def test_shared_findings_are_labelled_for_the_viewer_and_not_for_the_owner(
    client, people, alices, sqlite_connection
):
    query, chart = alices
    _approved(client, chart, people["alice"])

    mine = next(q for q in _summary(client, people["alice"])["queries"] if q["query_id"] == query["id"])
    theirs = next(q for q in _summary(client, people["bob"])["queries"] if q["query_id"] == query["id"])
    assert mine["shared"] is False
    assert theirs["shared"] is True

    own_section = _section(client, people["alice"], sqlite_connection["id"], query["id"])
    shared_section = _section(client, people["bob"], sqlite_connection["id"], query["id"])
    assert own_section["shared"] is False
    assert shared_section["shared"] is True
    assert shared_section["owner_name"] == "An Analyst"
    # A name, never an email.
    assert "alice@example.com" not in str(shared_section)


def test_an_admin_sees_every_query_and_it_reads_as_shared(client, people, alices, sqlite_connection):
    query, _ = alices
    section = _section(client, people["boss"], sqlite_connection["id"], query["id"])
    assert section["flagged_count"] == 2
    assert section["shared"] is True


def test_unpublishing_withdraws_the_alerts_again(client, people, alices):
    query, chart = alices
    _approved(client, chart, people["alice"])
    assert _count(client, people["bob"], query["id"]) == 2

    client.post(f"/queries/charts/{chart['id']}/unpublish", headers=people["alice"])

    assert _count(client, people["bob"], query["id"]) == 0


def test_the_viewers_poll_of_the_chart_carries_the_flag_marks(client, people, alices):
    _, chart = alices
    _approved(client, chart, people["alice"])
    body = _poll(client, people["bob"], chart["id"])
    assert body["flags"]["flagged_count"] == 2
    assert len(body["flags"]["rows"]) == 2


# --- personal dismissals ---------------------------------------------------


def test_a_viewers_dismissal_hides_it_for_the_viewer_only(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]

    response = client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"],
        json={"fingerprints": [victim]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == 1

    assert victim not in _rows(client, people["bob"], sqlite_connection["id"], query["id"])
    assert _count(client, people["bob"], query["id"]) == 1
    # Everybody else still has all of it.
    for who in ("alice", "boss", "carol"):
        assert victim in _rows(client, people[who], sqlite_connection["id"], query["id"]), who
        assert _count(client, people[who], query["id"]) == 2, who


def test_the_authors_dismissal_no_longer_hides_it_from_anyone_else(
    client, people, alices, sqlite_connection
):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["alice"], sqlite_connection["id"], query["id"])[0]

    client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["alice"],
        json={"fingerprints": [victim]},
    )

    assert _count(client, people["alice"], query["id"]) == 1
    assert _count(client, people["bob"], query["id"]) == 2
    assert victim in _rows(client, people["bob"], sqlite_connection["id"], query["id"])


def test_a_dismissal_does_not_delete_the_stored_finding(client, people, alices, sqlite_connection, session):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    before = session.scalar(select(func.count()).select_from(FlaggedRow))

    client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"],
        json={"fingerprints": [victim]},
    )

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(FlaggedRow)) == before


def test_a_dismissal_is_recorded_against_the_person_who_made_it(client, people, alices, sqlite_connection, session):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json={"fingerprints": [victim]}
    )
    bob = client.get("/auth/me", headers=people["bob"]).json()["id"]

    session.expire_all()
    [row] = session.query(FlagDismissal).all()
    assert row.user_id == bob
    assert row.row_fingerprint == victim


def test_two_people_can_dismiss_the_same_finding_independently(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    body = {"fingerprints": [victim]}

    first = client.post(f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json=body).json()
    second = client.post(f"/queries/{query['id']}/flag-dismissals", headers=people["carol"], json=body).json()

    # Carol's is new to her even though Bob already dismissed it.
    assert first["changed"] == 1
    assert second["changed"] == 1
    # Bob again: a no-op, as before.
    again = client.post(f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json=body).json()
    assert again["changed"] == 0


def test_a_viewers_restore_brings_it_back_for_the_viewer_only(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    for who in ("bob", "carol"):
        client.post(
            f"/queries/{query['id']}/flag-dismissals", headers=people[who], json={"fingerprints": [victim]}
        )

    restored = client.delete(f"/queries/{query['id']}/flag-dismissals", headers=people["bob"])

    assert restored.status_code == 200, restored.text
    assert restored.json()["changed"] == 1
    assert _count(client, people["bob"], query["id"]) == 2
    # Carol's own dismissal is untouched.
    assert _count(client, people["carol"], query["id"]) == 1


def test_the_viewers_poll_follows_their_own_dismissals(client, people, alices, sqlite_connection):
    """The rendered-response cache must not hand one person's dismissed view to
    another: the dismissal state is part of the hash it is keyed on."""
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json={"fingerprints": [victim]}
    )

    bobs = _poll(client, people["bob"], chart["id"])
    carols = _poll(client, people["carol"], chart["id"])
    alices_own = client.get(f"/queries/{query['id']}/poll?force=true", headers=people["alice"]).json()

    assert bobs["flags"]["flagged_count"] == 1
    assert victim not in [r["fingerprint"] for r in bobs["flags"]["rows"]]
    assert carols["flags"]["flagged_count"] == 2
    assert alices_own["flags"]["flagged_count"] == 2
    assert bobs["data_hash"] != carols["data_hash"]


def test_a_dismissal_survives_the_next_run_for_the_dismisser_only(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json={"fingerprints": [victim]}
    )

    client.post(f"/queries/{query['id']}/run", headers=people["alice"])

    assert _count(client, people["bob"], query["id"]) == 1
    assert _count(client, people["carol"], query["id"]) == 2


# --- what a viewer still cannot do ----------------------------------------


def test_a_viewer_cannot_dismiss_on_an_unpublished_query(client, people, alices, sqlite_connection):
    query, _ = alices
    victim = _rows(client, people["alice"], sqlite_connection["id"], query["id"])[0]
    response = client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json={"fingerprints": [victim]}
    )
    assert response.status_code == 404
    assert client.delete(f"/queries/{query['id']}/flag-dismissals", headers=people["bob"]).status_code == 404


def test_a_viewer_cannot_clear_stored_findings_or_edit_rules(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])

    assert client.delete(f"/queries/{query['id']}/flagged-rows", headers=people["bob"]).status_code == 404
    assert client.put(
        f"/queries/{query['id']}/flag-rules", headers=people["bob"], json={"rules": []}
    ).status_code == 404
    assert _count(client, people["alice"], query["id"]) == 2


def test_a_viewers_refresh_never_runs_somebody_elses_query(client, people, alices, sqlite_connection):
    query, chart = alices
    _approved(client, chart, people["alice"])
    before = len(client.get(f"/queries/{query['id']}/logs", headers=people["alice"]).json())

    body = client.post(
        f"/connections/{sqlite_connection['id']}/flagged/refresh", headers=people["bob"]
    ).json()

    assert len(client.get(f"/queries/{query['id']}/logs", headers=people["alice"]).json()) == before
    # The shared section still comes back, from what is stored.
    section = next(s for s in body["queries"] if s["query_id"] == query["id"])
    assert section["flagged_count"] == 2
    assert section["error_code"] is None


def test_an_owners_refresh_still_runs_their_own_query(client, people, alices, sqlite_connection):
    query, _ = alices
    before = len(client.get(f"/queries/{query['id']}/logs", headers=people["alice"]).json())
    client.post(f"/connections/{sqlite_connection['id']}/flagged/refresh", headers=people["alice"])
    assert len(client.get(f"/queries/{query['id']}/logs", headers=people["alice"]).json()) == before + 1


# --- the database ----------------------------------------------------------


def test_a_dismissal_goes_with_its_user(client, people, alices, sqlite_connection, session):
    """CASCADE: it is that person's own reading state."""
    query, chart = alices
    _approved(client, chart, people["alice"])
    victim = _rows(client, people["bob"], sqlite_connection["id"], query["id"])[0]
    client.post(
        f"/queries/{query['id']}/flag-dismissals", headers=people["bob"], json={"fingerprints": [victim]}
    )
    bob = client.get("/auth/me", headers=people["bob"]).json()["id"]
    from app.features.users.models import User

    session.execute(User.__table__.delete().where(User.id == bob))
    session.commit()
    assert session.query(FlagDismissal).count() == 0
    assert make_user and login  # imports kept honest
