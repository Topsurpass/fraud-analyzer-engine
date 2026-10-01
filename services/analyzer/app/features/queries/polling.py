"""Running and polling one saved query: the shared core of every read path.

The poll endpoint, the batch poll, the run endpoint and a published chart's poll
all reach the target database (or the cache in front of it) through here, so the
caching, hashing and logging cannot drift between them. HTTP concerns (auth,
gzip, response shape) stay in ``router.py``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.errors import AppError
from app.features.connections import service as connection_service
from app.features.flag_rules import (
    dismissals as flag_dismissal_service,
)
from app.features.flag_rules import (
    flagged_rows as flagged_row_service,
)
from app.features.queries import (
    execution as query_service,
)
from app.features.queries import (
    refresher,
    result_cache,
)
from app.features.queries import service as svc
from app.features.queries.models import SavedQuery
from app.features.queries.schemas import PollUnchanged
from app.features.users.models import User


def to_run_response(payload, poll_interval_ms: int) -> dict:
    return payload.as_dict(poll_interval_ms)


def execute_and_log(session: Session, query, conn, user: User):
    """Run a saved query, recording the attempt either way.

    ``user`` is who asked for this run, threaded all the way down to the log
    row so "who ran this" is answerable later. It is never None for these two
    call sites - both sit behind ``require_user``. Every other path that
    executes against a target carries its caller too, including the ones that
    finish after the response: the stale-cache refresher takes the id of the
    analyst whose poll started it, and the flagged refresh the id of whoever
    clicked. Only ``app/features/queries/scheduler.py`` logs with no user, because a
    timer really has nobody behind it.
    """
    try:
        payload = query_service.run_saved_query(query, conn)
    except AppError as error:
        svc.log_execution(session, query.id, success=False, error=error, user_id=user.id)
        raise
    svc.log_execution(
        session,
        query.id,
        success=True,
        row_count=payload.row_count,
        duration_ms=payload.duration_ms,
        user_id=user.id,
    )
    # Every actual execution updates the stored queue, so a finding outlives
    # the cache entry that produced it and the flagged view has something to
    # show without re-running anything.
    flagged_row_service.sync(session, query, payload.columns, payload.rows, payload.flags)
    return payload


def poll_one(
    session: Session,
    query: SavedQuery,
    since_hash: str | None,
    force: bool,
    user: User,
) -> dict:
    """The whole poll decision for one query.

    Takes an already-resolved query rather than an id, because callers earn
    the right to it in different ways: an owner through ``get_owned``, a
    viewer of a published chart through the chart being public. Keeping the
    resolution outside means the caching, hashing and logging below cannot
    drift between those paths.
    """
    query_id = query.id
    interval = query_service.poll_interval_for(query)
    # Read once and applied to whichever payload is served. A row the analyst
    # has reviewed should stop being marked on the chart too, not only in the
    # flagged view - a card still showing it red is telling them there is work
    # left that they have already done. The *caller's* dismissals, not the
    # query's: on a published chart each viewer clears their own.
    dismissed = flag_dismissal_service.dismissed_fingerprints(session, query_id, user.id)

    # A fresh entry answers outright. A stale one answers *and* starts a
    # refresh behind the response: the reader gets the last known chart
    # immediately instead of waiting on the target database, and the next poll
    # picks up the new one when it lands. Only a query nobody has ever run
    # blocks, and only once.
    cached = None if force else result_cache.get(query_id)
    if cached is None and not force:
        stale = result_cache.get_stale(query_id)
        if stale is not None:
            # The person polling is the person who caused the refresh, even
            # though it lands after their response does.
            refresher.request_refresh(query_id, user.id)
            cached = stale

    if cached is not None:
        body = flag_dismissal_service.apply_dismissals(cached.payload, dismissed)
        if since_hash and body["data_hash"] == since_hash:
            return PollUnchanged(
                query_id=query_id,
                data_hash=body["data_hash"],
                poll_interval_ms=interval,
                from_cache=True,
                executed_at=body.get("executed_at"),
            ).model_dump()
        return {**body, "changed": True, "from_cache": True}

    conn = connection_service.get_connection(session, query.connection_id)
    payload = execute_and_log(session, query, conn, user)
    body = to_run_response(payload, interval)
    # Cached unfiltered, on purpose: a later dismissal has to be able to change
    # what this payload looks like without the query being run again.
    result_cache.set(query_id, payload.data_hash, body, ttl_ms=interval)
    body = flag_dismissal_service.apply_dismissals(body, dismissed)

    if since_hash and body["data_hash"] == since_hash:
        return PollUnchanged(
            query_id=query_id,
            data_hash=body["data_hash"],
            poll_interval_ms=interval,
            from_cache=False,
            executed_at=body.get("executed_at"),
        ).model_dump()
    return {**body, "changed": True, "from_cache": False}
