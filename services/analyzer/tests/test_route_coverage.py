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

This sweep walks ``APIRoute`` objects only. It does not expand ``Mount``
objects or websocket routes - neither exists in this app today, so the gap is
latent rather than live, but a future author adding either should not assume
this test's green run means every path is covered.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from app.main import app
from app.routers.auth import current_user
from app.security.deps import PUBLIC_PATHS, require_admin, require_user

#: Paths guarded by bare ``current_user`` rather than ``require_user``, each
#: one a named, reviewable exception - not a blanket exemption.
#:
#: ``current_user`` authenticates (401 with no session) but does not enforce
#: the ``must_change_password`` gate; only ``require_user`` does. Routing
#: ``/auth/change-password`` through ``require_user`` would trap a user
#: holding a temporary password with no way to clear the flag - it is the one
#: endpoint that must stay reachable *because* the gate is up, or the gate has
#: no way out. ``/auth/me`` needs the same exemption for the read side: a
#: client restoring a session from a stored token, or an admin who forces the
#: flag onto a session that predates the change, needs to be able to ask "who
#: am I, and do I need to change my password" before the gate blocks
#: everything else. No other route belongs here - accepting ``current_user``
#: globally would silently pass a future endpoint that authenticates but never
#: checks the password gate, which is exactly the hole this file exists to
#: catch.
_CURRENT_USER_ONLY_PATHS = frozenset({"/auth/me", "/auth/change-password"})


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
    if route.path in _CURRENT_USER_ONLY_PATHS:
        assert current_user in functions, (
            f"{route.path} is on _CURRENT_USER_ONLY_PATHS but does not use "
            f"current_user. Wire it to current_user, or remove it from the "
            f"allowlist if it should now go through require_user/require_admin."
        )
        return
    assert require_user in functions or require_admin in functions, (
        f"{route.path} has no authentication dependency that enforces the "
        f"password-change gate. Add require_user or require_admin to its "
        f"router, or - only if it must remain reachable under that gate, the "
        f"way /auth/me and /auth/change-password are - add the path to "
        f"_CURRENT_USER_ONLY_PATHS with a comment explaining why."
    )


def test_the_public_allowlist_stays_small():
    """Every entry is a decision. Growth here should be noticed."""
    assert PUBLIC_PATHS == frozenset(
        {"/health", "/ready", "/auth/login", "/auth/logout"}
    )


def test_the_current_user_only_allowlist_stays_small():
    """Same discipline as PUBLIC_PATHS: growth here should be noticed, not
    absorbed by a broader acceptance rule."""
    assert _CURRENT_USER_ONLY_PATHS == frozenset(
        {"/auth/me", "/auth/change-password"}
    )
