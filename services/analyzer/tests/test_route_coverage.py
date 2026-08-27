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

The sweep covers *every* route object the app serves, not only the
``APIRoute`` ones. It used to skip anything that was not an ``APIRoute``, and
that silently dropped four live, unauthenticated endpoints: FastAPI registers
``/docs``, ``/docs/oauth2-redirect``, ``/redoc`` and ``/openapi.json`` as
plain ``starlette.routing.Route`` objects, so they were neither guarded nor
allowlisted, and this file's stated contract was false while it ran green.
They are now a recorded decision on ``PUBLIC_PATHS``, and anything else that
is not an ``APIRoute`` - a ``Mount``, a websocket, another framework-supplied
route - fails the sweep unless it is allowlisted too. A non-``APIRoute`` has
no ``dependant``, so there is no way to prove it is guarded; the only
defensible verdict on one is a human decision written down on the allowlist,
which is what ``test_every_non_api_route_is_an_explicit_decision`` demands.
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
            # Counted by _unlisted_non_api_routes below rather than dropped.
            continue
        if route_context.path in PUBLIC_PATHS:
            continue
        routes.append(route_context)
    return routes


def _unlisted_non_api_routes(routes=None) -> list[str]:
    """Paths served by something other than an ``APIRoute`` and not allowlisted.

    Takes ``routes`` so the planted-route test can hand in a route table
    without mutating the real app, which would leak into every other test in
    the session through the module-level ``app`` import.
    """
    return [
        route_context.path
        for route_context in iter_route_contexts(
            app.routes if routes is None else routes
        )
        if not isinstance(route_context.original_route, APIRoute)
        and route_context.path not in PUBLIC_PATHS
    ]


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


def test_every_non_api_route_is_an_explicit_decision():
    """A route that is not an ``APIRoute`` cannot be proved guarded - it has no
    ``dependant`` to inspect - so the only honest verdict is a written
    decision. Skipping them, which this file used to do, meant four live
    unauthenticated endpoints were neither checked nor recorded."""
    assert _unlisted_non_api_routes() == []


def test_a_planted_unguarded_non_api_route_is_caught():
    """The sweep's whole purpose is the route somebody adds next year. A plain
    Starlette route used to pass through it invisibly; this proves it no
    longer does."""
    from starlette.routing import Route as StarletteRoute

    async def _endpoint(request):  # pragma: no cover - never called
        raise AssertionError("planted route must not be served")

    planted = StarletteRoute("/planted-and-open", endpoint=_endpoint)

    assert _unlisted_non_api_routes([*app.routes, planted]) == ["/planted-and-open"]
    # And the real table is untouched by the probe.
    assert _unlisted_non_api_routes() == []


def test_a_planted_non_api_route_passes_once_it_is_allowlisted(monkeypatch):
    """The escape hatch has to work, or the rule above becomes a reason to
    delete the check rather than record the decision."""
    from starlette.routing import Route as StarletteRoute

    import tests.test_route_coverage as module

    async def _endpoint(request):  # pragma: no cover - never called
        raise AssertionError("planted route must not be served")

    planted = StarletteRoute("/planted-and-declared", endpoint=_endpoint)
    monkeypatch.setattr(module, "PUBLIC_PATHS", PUBLIC_PATHS | {"/planted-and-declared"})

    assert module._unlisted_non_api_routes([*app.routes, planted]) == []


def test_the_public_allowlist_stays_small():
    """Every entry is a decision. Growth here should be noticed.

    The four documentation paths are FastAPI's own, they answer without a
    session today, and they are listed so that is a recorded exposure rather
    than an invisible one - see the comment on ``PUBLIC_PATHS``.
    """
    assert PUBLIC_PATHS == frozenset(
        {
            "/health",
            "/ready",
            "/auth/login",
            "/auth/logout",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/openapi.json",
        }
    )


def test_the_current_user_only_allowlist_stays_small():
    """Same discipline as PUBLIC_PATHS: growth here should be noticed, not
    absorbed by a broader acceptance rule."""
    assert _CURRENT_USER_ONLY_PATHS == frozenset(
        {"/auth/me", "/auth/change-password"}
    )
