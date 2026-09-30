"""Chart endpoints: a query's chart set, and publishing charts to everyone.

The chart types themselves are listed in ``app/policy/chart_types.py``.

Mounted under ``/queries`` with the ``queries`` tag so the API surface is the same
as when these lived beside the query routes. Registered before the query router
in ``app/main.py``: ``/queries/charts/published`` must be matched as a literal
before ``/queries/{query_id}`` can claim it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.features.charts import service as query_chart_service
from app.features.charts.schemas import (
    QueryChartRead,
    QueryChartSetRead,
    QueryChartSetUpdate,
)
from app.features.queries import service as svc
from app.features.queries.polling import poll_one
from app.features.users.models import User
from app.security.deps import require_user

query_scoped = APIRouter(
    prefix="/queries", tags=["queries"], dependencies=[Depends(require_user)]
)


@query_scoped.post("/charts/{chart_id}/publish", response_model=QueryChartRead)
def publish_chart(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Share one chart with every signed-in user.

    An analyst may publish a chart on a query they own; an admin may publish
    anyone's. Publishing freezes the query behind it, so a colleague reading
    the chart cannot have the definition changed under them.
    """
    return QueryChartRead.model_validate(
        query_chart_service.publish(session, chart_id, user)
    )


@query_scoped.post("/charts/{chart_id}/unpublish", response_model=QueryChartRead)
def unpublish_chart(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Retract a publication, unfreezing the query behind it.

    Whoever published may unpublish, and an admin always may. So an analyst can
    retract their own, edit, and republish - viewers see the chart leave and
    return changed, which is visible rather than silent. A chart an admin
    published stays the admin's to retract.
    """
    return QueryChartRead.model_validate(
        query_chart_service.unpublish(session, chart_id, user)
    )


@query_scoped.get("/charts/{chart_id}/poll")
def poll_published_chart(
    chart_id: str,
    since_hash: str | None = Query(default=None),
    force: bool = Query(default=False, description="Bypass the cache"),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    """The rows behind one published chart, for anybody signed in.

    A published chart that a viewer cannot poll renders an empty card, so
    sharing the chart without sharing its result would be sharing nothing.
    This is the only read path in the app that deliberately ignores ownership,
    and it is narrow on purpose: the chart must actually be published, and
    what comes back is trimmed to that one chart.

    Two things are deliberately withheld. The SQL text never appears in a run
    payload at all, so a viewer sees the result without the query behind it.
    And the payload's chart list is filtered to the published chart alone,
    because the query's other charts were not shared and a viewer has no
    business learning they exist.
    """
    chart = query_chart_service.get_published(session, chart_id)
    payload = poll_one(session, chart.query, since_hash, force, user)

    charts = payload.get("charts")
    if isinstance(charts, list):
        payload = {
            **payload,
            "charts": [spec for spec in charts if spec.get("id") == chart_id],
        }
    return payload


@query_scoped.get("/charts/published", response_model=list[QueryChartRead])
def list_published_charts(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[QueryChartRead]:
    """Every published chart, whoever built it.

    Deliberately unfiltered by owner: published means published, and escaping
    the owner-only rule is the entire point of the feature.
    """
    return [
        QueryChartRead.model_validate(chart)
        for chart in query_chart_service.list_published(session)
    ]


@query_scoped.get("/{query_id}/charts", response_model=QueryChartSetRead)
def get_charts(
    query_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartSetRead:
    """Every way this query's result can be drawn, in display order."""
    svc.get_owned(session, query_id, user)
    return QueryChartSetRead(
        query_id=query_id, charts=query_chart_service.list_charts(session, query_id)
    )


@query_scoped.put("/{query_id}/charts", response_model=QueryChartSetRead)
def put_charts(
    query_id: str,
    payload: QueryChartSetUpdate,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartSetRead:
    """Replace a query's whole chart set.

    Charts are matched by name, so editing a chart's type or fields keeps its
    id and every dashboard placing it keeps working. Renaming one reads as
    removing it and adding another, which is the honest interpretation: a board
    placing "Trend" cannot know the thing now called "Volume" is the same
    intent.

    Adding a chart costs nothing at the target database. They all render from
    one already-fetched result, which is the entire point of separating them
    from the query.
    """
    query = svc.get_owned(session, query_id, user)
    # Replacing the set can rewire a published chart's fields or drop it
    # entirely, both of which change what viewers see.
    query_chart_service.guard_frozen(session, query, user)
    charts = query_chart_service.replace_charts(session, query, payload.charts)
    return QueryChartSetRead(query_id=query_id, charts=charts)
