"""No route reaches the database without a signed-in user behind it.

This is the highest-value test in the authentication work, because it is the
one that stays correct as the app grows. Every other test here checks a rule
that exists today; this one fails when somebody adds an endpoint next year and
forgets the dependency. A hole introduced that way is invisible in review - the
new code looks exactly like the code beside it.

A note on how this walks the route table: this FastAPI version does not put a
flat ``APIRoute`` per endpoint on ``app.routes``. ``app.include_router(...)``
instead pushes an opaque ``_IncludedRouter`` wrapper, and the concrete,
dependency-resolved route only comes into existence when something asks for
the *effective* route - which is exactly what ``fastapi.openapi.utils.get_openapi``
does to build ``/openapi.json``, via ``fastapi.routing.iter_route_contexts``.
That is the same public seam this test uses: it is not reaching past FastAPI's
back into an internal structure, it is calling the function FastAPI itself
calls to answer "what does this app actually serve", which is the question
this test is asking.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from app.main import app
from app.routers.auth import current_user
from app.security.deps import PUBLIC_PATHS, require_admin, require_user

#: Functions that count as "this route is behind a signed-in session".
#:
#: ``require_user`` and ``require_admin`` are the two every router in
#: app/routers/ (other than auth.py itself) is built on. ``current_user`` is
#: included too: it is the dependency ``/auth/me`` and
#: ``/auth/change-password`` use instead of ``require_user``, deliberately -
#: see the long comment on ``PUBLIC_PATHS`` in ``app/security/deps.py`` for
#: why those two routes cannot be routed through the password-change gate
#: without breaking the one path a locked account has out of it. Accepting
#: ``current_user`` here does not weaken the sweep: it still refuses an
#: unauthenticated caller with 401, so no route this test passes is reachable
#: without a real session. What it does not do is authorise a role, which is
#: exactly right for those two endpoints - they answer "who is this", not
#: "is this person allowed to do X".
_ACCEPTED_GUARDS = {current_user, require_user, require_admin}


def _dependency_functions(route: APIRoute) -> set:
    return {d.call for d in route.dependant.dependencies}


def _guarded_routes() -> list[APIRoute]:
    routes = []
    for route_context in iter_route_contexts(app.routes):
        original = route_context.original_route
        if not isinstance(original, APIRoute):
            continue
        if route_context.path in PUBLIC_PATHS:
            continue
        routes.append(route_context)
    return routes


def test_there_are_routes_to_check():
    """Guards the guard. If the collection breaks and returns nothing, every
    assertion below passes vacuously and this file becomes decoration."""
    assert len(_guarded_routes()) > 15


@pytest.mark.parametrize("route", _guarded_routes(), ids=lambda r: f"{r.path}")
def test_every_route_requires_a_signed_in_user(route):
    functions = _dependency_functions(route)
    assert functions & _ACCEPTED_GUARDS, (
        f"{route.path} has no authentication dependency. Add require_user or "
        f"require_admin to its router, or add the path to PUBLIC_PATHS with a "
        f"comment explaining why it is safe to expose."
    )


def test_the_public_allowlist_stays_small():
    """Every entry is a decision. Growth here should be noticed."""
    assert PUBLIC_PATHS == frozenset(
        {"/health", "/ready", "/auth/login", "/auth/logout"}
    )
