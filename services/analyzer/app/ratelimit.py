"""Per-client request budgets.

There is no authentication on this service, by design and by scope. That makes
a rate limit the only thing standing between the open internet and an endpoint
that runs caller-supplied SQL against a customer's production database. It is
containment, not authentication: it bounds what an anonymous caller can spend,
it does not decide who they are.

Two buckets, because the endpoints differ by orders of magnitude in cost:

* **execution** -- ``/run``, ``/poll``, ``/query/preview`` and saved-query
  writes, all of which open a connection to a target database. The budget has
  to absorb a legitimate dashboard: twelve cards at the five-second default is
  144 polls a minute from a single browser, so this cannot be set tight.
* **general** -- everything else: CRUD, introspection, health.

Fixed windows rather than a sliding log. A sliding window means storing a
timestamp per request; a fixed window is two integers per client and its only
inaccuracy is that a caller can spend two budgets across a window boundary.
For a containment control that is a fair trade, and the alternative would grow
memory in proportion to traffic, which is itself the thing being defended
against.

State is per-process and dies with it. Behind several instances each gets its
own budget, so the effective limit multiplies by instance count. That is
documented rather than solved: solving it means shared state, and a Redis
dependency is not worth it for a single-tenant tool.
"""

from __future__ import annotations

import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings
from app.errors import ErrorCode

WINDOW_SECONDS = 60

#: Path fragments that mark a request as touching a target database.
_EXECUTION_MARKERS = (
    "/run",
    "/poll",
    "/query/preview",
    "/queries",
    "/test",
    "/flagged/refresh",
)


def _is_execution(path: str, method: str) -> bool:
    """Classify a request into the expensive bucket.

    ``GET /queries/{id}`` is metadata and cheap; ``POST`` to the same prefix
    dry-runs SQL against the target and is not.
    """
    if "/run" in path or "/poll" in path or path.endswith("/query/preview"):
        return True
    if path.endswith("/test"):
        return True
    # One click here re-runs every rule-bearing query on the connection. It
    # matches none of the markers above -- no /run, no /queries in the path --
    # so without this line the single most expensive endpoint in the service
    # would be charged to the cheap bucket.
    if path.endswith("/flagged/refresh"):
        return True
    if "/queries" in path and method in ("POST", "PUT"):
        # Saving a rule set writes to the app-state database and never reaches
        # the target, so it does not belong in the bucket that exists to bound
        # load on a customer's production server.
        if path.endswith("/flag-rules"):
            return False
        return True
    return False


class _Window:
    """Fixed-window counters, keyed by client, for one bucket."""

    __slots__ = ("_counts", "_lock")

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, now: float) -> tuple[bool, int]:
        """Record a request. Returns (allowed, seconds until the window resets)."""
        window = int(now // WINDOW_SECONDS)
        reset_in = WINDOW_SECONDS - int(now % WINDOW_SECONDS)
        with self._lock:
            current_window, count = self._counts.get(key, (window, 0))
            if current_window != window:
                current_window, count = window, 0
            count += 1
            self._counts[key] = (current_window, count)

            # Bound memory: drop keys whose window has passed. Cheap because it
            # only runs when the table is already large.
            if len(self._counts) > 10_000:
                self._counts = {
                    k: v for k, v in self._counts.items() if v[0] == window
                }

        return count <= limit, reset_in

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


_general = _Window()
_execution = _Window()


def reset() -> None:
    """Drop all counters. For tests, and for nothing else."""
    _general.clear()
    _execution.clear()


def client_key(request: Request) -> str:
    """Identify the caller.

    ``X-Forwarded-For``'s first entry is used when present, because behind
    Fly's proxy every request otherwise arrives from the same peer address and
    the whole service would share one budget. The header is client-controlled
    and therefore spoofable; that is acceptable for a containment control and
    is exactly why this is not an authentication mechanism.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        execution = _is_execution(request.url.path, request.method)
        limit = (
            settings.rate_limit_execution_per_minute
            if execution
            else settings.rate_limit_per_minute
        )

        if limit <= 0:  # 0 disables the bucket entirely.
            return await call_next(request)

        window = _execution if execution else _general
        allowed, reset_in = window.hit(client_key(request), limit, time.time())

        if not allowed:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(reset_in)},
                content={
                    "error_code": ErrorCode.RATE_LIMITED.value,
                    "message": (
                        f"Rate limit of {limit} requests per minute exceeded. "
                        f"Retry in {reset_in}s."
                    ),
                    "detail": {
                        "limit_per_minute": limit,
                        "bucket": "execution" if execution else "general",
                        "retry_after_s": reset_in,
                    },
                },
            )

        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Refuse an oversized body before it is read into memory.

    The SQL guard caps statement length, but only after Starlette has already
    buffered and JSON-decoded the whole body. Checking Content-Length first
    means a multi-megabyte POST costs a header parse instead of a multi-
    megabyte allocation, which matters because nothing here is authenticated.

    A request without Content-Length (chunked) is passed through: the ASGI
    server's own limits apply there, and guessing would break legitimate
    clients.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        limit = get_settings().max_request_bytes
        if limit > 0:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error_code": ErrorCode.REQUEST_VALIDATION_ERROR.value,
                        "message": (
                            f"Request body is {declared} bytes, over the "
                            f"{limit} byte limit."
                        ),
                        "detail": {"max_request_bytes": limit},
                    },
                )
        return await call_next(request)
