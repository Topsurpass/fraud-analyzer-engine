"""The dependencies every router hangs its authorisation on.

Applied at the router rather than the endpoint. An endpoint-level dependency is
a thing somebody has to remember on the day they add a route; a router-level one
is inherited by construction, and ``tests/test_route_coverage.py`` fails the
build if a router ever ships without one.
"""

from __future__ import annotations

from fastapi import Depends

from app.errors import AppError, ErrorCode
from app.models.user import User
from app.routers.auth import current_user

#: Routes that answer without a session, each one a deliberate decision.
#:
#: ``/health`` so a load balancer can probe without credentials. ``/ready``
#: for the same reason: ``fly.toml`` and the Dockerfile's own HEALTHCHECK
#: both hit it with a bare, unauthenticated request, so gating it behind a
#: session would make every deploy look unhealthy to the platform that is
#: deciding whether to route traffic to it. ``/auth/login`` because it is
#: where credentials are exchanged. ``/auth/logout`` because clearing an
#: already-expired session must succeed rather than 401.
#:
#: ``/auth/me`` and ``/auth/change-password`` are deliberately NOT on this
#: list, and are also deliberately not wired to ``require_user`` below: both
#: sit behind ``current_user`` (imported above, defined in
#: ``app/routers/auth.py``), which is a real, strictly weaker guard - a valid
#: session, full stop, still 401 with none - that ``require_user`` layers the
#: password-change gate on top of. ``/auth/change-password`` is the one
#: endpoint an account with ``must_change_password`` set must still be able to
#: reach, or the gate has no way out: wiring it to ``require_user`` would 403
#: the very call that clears the flag. ``/auth/me`` stays on ``current_user``
#: for the matching reason on the read side - a client restoring a session
#: from a stored token, or an admin who forces the flag on a session that
#: predates the change, needs to be able to ask "who am I, and do I need to
#: change my password" before the gate gets in the way of anything else.
#: ``tests/test_route_coverage.py`` accepts ``current_user`` as a satisfying
#: dependency for exactly this reason, so both stay covered by the sweep
#: without being misclassified as reachable with no session at all.
PUBLIC_PATHS = frozenset({"/health", "/ready", "/auth/login", "/auth/logout"})


def require_user(user: User = Depends(current_user)) -> User:
    """A signed-in, active user who has finished setting up their account.

    The password-change gate lives here rather than in each router because a
    restricted session that can still call the API is not restricted. The
    frontend declining to show other screens is presentation, not a control.
    """
    if user.must_change_password:
        raise AppError(
            ErrorCode.PASSWORD_CHANGE_REQUIRED,
            "Choose a new password before continuing.",
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """An administrator.

    The message says what is required rather than merely refusing, so a person
    who has been given the wrong role finds out why instead of filing a bug.
    """
    if not user.is_admin:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "This needs an administrator account.",
        )
    return user
