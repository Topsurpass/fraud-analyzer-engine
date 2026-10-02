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
    ChartDefinitionRead,
    PublishApproveRequest,
    PublishRejectRequest,
    PublishRequestRead,
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


@query_scoped.get("/charts/publish-requests", response_model=list[PublishRequestRead])
def list_publish_requests(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[PublishRequestRead]:
    """Every chart waiting for an administrator's decision, oldest first.

    Administrators only. Registered before ``/charts/{chart_id}/...`` so the
    literal is matched first.
    """
    return [
        PublishRequestRead.model_validate(request)
        for request in query_chart_service.list_pending(session, user)
    ]


@query_scoped.post("/charts/{chart_id}/publish", response_model=QueryChartRead)
def publish_chart(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Publish a chart (administrators) or ask for it to be published (everyone
    else).

    An administrator publishes at once, anyone's chart. An analyst's request
    makes the chart ``pending`` until an administrator approves or rejects it.
    Either way the query behind it is frozen, so nobody reading the chart - or
    deciding on it - can have the definition changed under them.
    """
    return QueryChartRead.model_validate(
        query_chart_service.publish(session, chart_id, user)
    )


@query_scoped.post("/charts/{chart_id}/publish/cancel", response_model=QueryChartRead)
def cancel_publish_request(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Withdraw a pending request, or clear a rejection notice. Unfreezes the query."""
    return QueryChartRead.model_validate(
        query_chart_service.cancel_request(session, chart_id, user)
    )


@query_scoped.post("/charts/{chart_id}/publish/approve", response_model=QueryChartRead)
def approve_publish_request(
    chart_id: str,
    payload: PublishApproveRequest,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Accept a request: the chart becomes visible to everyone signed in.

    Administrators only. The body carries the ``definition_fingerprint`` the
    administrator was shown (on ``GET /publish-requests`` and the definition):
    approval is bound to the definition that was reviewed, so a definition that has
    changed since is ``409 DEFINITION_CHANGED``. 409 ``PUBLISH_NOT_PENDING`` when
    nobody is waiting on it, and that is checked first.
    """
    return QueryChartRead.model_validate(
        query_chart_service.approve(session, chart_id, user, payload.definition_fingerprint)
    )


@query_scoped.post("/charts/{chart_id}/publish/reject", response_model=QueryChartRead)
def reject_publish_request(
    chart_id: str,
    payload: PublishRejectRequest | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> QueryChartRead:
    """Decline a request, with an optional reason the author will see.

    Administrators only. 409 ``PUBLISH_NOT_PENDING`` when nobody is waiting on it.
    """
    reason = payload.reason if payload is not None else None
    return QueryChartRead.model_validate(
        query_chart_service.reject(session, chart_id, user, reason)
    )


@query_scoped.get("/charts/{chart_id}/definition", response_model=ChartDefinitionRead)
def get_chart_definition(
    chart_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ChartDefinitionRead:
    """The SQL and configuration behind a chart, read-only.

    For anyone when the chart is published, so they can replicate it; for the
    author and administrators in every state, so an approver reads what they are
    approving. Carries the connection's name and nothing else about it, and a
    list's name without its items. There is no write endpoint behind this one.
    """
    return ChartDefinitionRead.model_validate(
        query_chart_service.definition(session, chart_id, user)
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
