"""Publishing a chart, and the freeze it puts on the query behind it."""

from __future__ import annotations

from app.models.enums import UserRole
from tests.test_auth_api import login, make_user

SQL = "SELECT day, count(*) AS n FROM txns GROUP BY day ORDER BY day"


def _auth(client, email, role):
    make_user(email=email, role=role)
    return {"Authorization": f"Bearer {login(client, email=email).json()['token']}"}


def _query_with_chart(client, auth, connection, name="Mine"):
    query = client.post(
        f"/connections/{connection['id']}/queries",
        headers=auth,
        json={"name": name, "sql_text": SQL},
    ).json()
    charts = client.put(
        f"/queries/{query['id']}/charts",
        headers=auth,
        json={"charts": [{"name": f"{name} chart", "chart_type": "table"}]},
    ).json()["charts"]
    return query, charts[0]


def test_an_analyst_publishes_a_chart_they_own(client, app_db, sqlite_connection):
    boss = _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)

    response = client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    assert response.status_code == 200, response.text
    assert response.json()["is_public"] is True
    assert response.json()["published_at"] is not None
    assert boss  # the admin exists so alice is not the last account


def test_publishing_is_visible_to_another_analyst(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    published = client.get("/queries/charts/published", headers=bob).json()

    # The whole point: it escapes the owner-only rule every other read obeys.
    assert [c["id"] for c in published] == [chart["id"]]


def test_an_unpublished_chart_stays_invisible_to_another_analyst(
    client, app_db, sqlite_connection
):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _query_with_chart(client, alice, sqlite_connection)

    assert client.get("/queries/charts/published", headers=bob).json() == []


def test_an_analyst_cannot_publish_another_analysts_chart(
    client, app_db, sqlite_connection
):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)

    response = client.post(f"/queries/charts/{chart['id']}/publish", headers=bob)

    # 404, not 403: confirming the chart exists tells bob what alice is doing.
    assert response.status_code == 404


def test_an_admin_can_publish_anyones_chart(client, app_db, sqlite_connection):
    boss = _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)

    assert client.post(f"/queries/charts/{chart['id']}/publish", headers=boss).status_code == 200


def test_publishing_freezes_the_query_for_its_owner(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    query, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    response = client.put(
        f"/queries/{query['id']}", headers=alice, json={"sql_text": "SELECT 1 AS n"}
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "QUERY_FROZEN"


def test_the_refusal_names_the_way_out(client, app_db, sqlite_connection):
    """"Frozen" with no next step sends somebody hunting for a setting that
    does not exist."""
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    query, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    body = client.put(
        f"/queries/{query['id']}", headers=alice, json={"sql_text": "SELECT 1 AS n"}
    ).json()

    assert "Unpublish" in body["message"]
    assert chart["name"] in body["message"]


def test_a_frozen_query_cannot_be_deleted_or_rewired(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    query, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    assert client.delete(f"/queries/{query['id']}", headers=alice).status_code == 409
    assert (
        client.put(
            f"/queries/{query['id']}/charts",
            headers=alice,
            json={"charts": [{"name": "Rewired", "chart_type": "table"}]},
        ).status_code
        == 409
    )


def test_an_admin_can_edit_a_frozen_query(client, app_db, sqlite_connection):
    """An admin is the approving authority, so requiring them to unpublish
    their own approval first is ceremony."""
    boss = _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    query, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    response = client.put(
        f"/queries/{query['id']}", headers=boss, json={"sql_text": "SELECT 1 AS n"}
    )

    assert response.status_code == 200, response.text


def test_unpublishing_unfreezes_the_query(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    query, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    client.post(f"/queries/charts/{chart['id']}/unpublish", headers=alice)

    assert client.put(
        f"/queries/{query['id']}", headers=alice, json={"sql_text": "SELECT 1 AS n"}
    ).status_code == 200


def test_an_analyst_cannot_unpublish_what_an_admin_published(
    client, app_db, sqlite_connection
):
    """This asymmetry is what makes an admin's freeze real rather than
    advisory: the author cannot simply undo it."""
    boss = _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    query, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=boss)

    response = client.post(f"/queries/charts/{chart['id']}/unpublish", headers=alice)

    assert response.status_code == 403
    assert client.put(
        f"/queries/{query['id']}", headers=alice, json={"sql_text": "SELECT 1 AS n"}
    ).status_code == 409


def test_an_admin_can_unpublish_anything(client, app_db, sqlite_connection):
    boss = _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    response = client.post(f"/queries/charts/{chart['id']}/unpublish", headers=boss)

    assert response.status_code == 200
    assert response.json()["is_public"] is False


def test_publishing_twice_is_harmless(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)

    first = client.post(f"/queries/charts/{chart['id']}/publish", headers=alice).json()
    second = client.post(f"/queries/charts/{chart['id']}/publish", headers=alice).json()

    # Re-publishing must not reassign the publisher, or an admin's freeze
    # could be taken over by the author simply clicking publish again.
    assert second["published_by"] == first["published_by"]
    assert second["published_at"] == first["published_at"]


def test_publishing_does_not_expose_the_sql(client, app_db, sqlite_connection):
    """A published chart shares the rendering and the result, not the query
    text behind it."""
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    body = client.get("/queries/charts/published", headers=bob).text

    assert SQL not in body


def test_a_viewer_can_actually_render_a_published_chart(client, app_db, sqlite_connection):
    """The bug this endpoint exists to fix.

    Listing a published chart without letting a viewer fetch its rows shares
    an empty card, which is sharing nothing.
    """
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    response = client.get(f"/queries/charts/{chart['id']}/poll?force=true", headers=bob)

    assert response.status_code == 200, response.text
    assert response.json()["row_count"] >= 0
    assert "columns" in response.json()


def test_a_viewer_cannot_poll_an_unpublished_chart(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)

    assert client.get(f"/queries/charts/{chart['id']}/poll", headers=bob).status_code == 404


def test_unpublishing_stops_the_viewer_poll(client, app_db, sqlite_connection):
    """An id remembered from before a retraction must stop working."""
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)
    assert client.get(f"/queries/charts/{chart['id']}/poll", headers=bob).status_code == 200

    client.post(f"/queries/charts/{chart['id']}/unpublish", headers=alice)

    assert client.get(f"/queries/charts/{chart['id']}/poll", headers=bob).status_code == 404


def test_the_viewer_poll_hides_the_querys_other_charts(client, app_db, sqlite_connection):
    """Publishing one chart shares one chart. The query's other charts were
    not shared, and a viewer has no business learning they exist."""
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    query = client.post(
        f"/connections/{sqlite_connection['id']}/queries",
        headers=alice,
        json={"name": "Two charts", "sql_text": SQL},
    ).json()
    charts = client.put(
        f"/queries/{query['id']}/charts",
        headers=alice,
        json={
            "charts": [
                {"name": "Shared one", "chart_type": "table"},
                {"name": "Private one", "chart_type": "table"},
            ]
        },
    ).json()["charts"]
    client.post(f"/queries/charts/{charts[0]['id']}/publish", headers=alice)

    body = client.get(f"/queries/charts/{charts[0]['id']}/poll?force=true", headers=bob).json()

    assert [c["id"] for c in body["charts"]] == [charts[0]["id"]]
    assert "Private one" not in str(body)


def test_the_viewer_poll_never_carries_the_sql(client, app_db, sqlite_connection):
    _auth(client, "boss@example.com", UserRole.ADMIN)
    alice = _auth(client, "alice@example.com", UserRole.ANALYST)
    bob = _auth(client, "bob@example.com", UserRole.ANALYST)
    _, chart = _query_with_chart(client, alice, sqlite_connection)
    client.post(f"/queries/charts/{chart['id']}/publish", headers=alice)

    body = client.get(f"/queries/charts/{chart['id']}/poll?force=true", headers=bob).text

    assert SQL not in body
