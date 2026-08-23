"""Logging configuration and per-request correlation.

Two problems this solves, both of which make a deployed failure undiagnosable.

**Nothing configured the root logger.** uvicorn's default logging config
touches only the ``uvicorn.*`` loggers, so the root logger stayed at WARNING
with nothing but ``logging.lastResort`` attached. Every ``logger.info`` in the
service was discarded, including the startup banner in :mod:`app.db.migrate`
that the README tells operators to read to confirm which backend a deployment
is actually using. That line was documented and unreachable.

**Nothing correlated a log line with a request.** ``main.handle_unexpected_error``
logs a traceback, but with no request id, path, or method attached there was no
way to connect a 500 in the logs to the call that caused it.

The request id is kept in a ``ContextVar``, which is the one mechanism that
survives FastAPI running sync endpoints on the anyio worker threadpool: the
context is copied into the worker thread, so a ``logger.exception`` deep inside
a service function still carries the id of the request that triggered it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings

#: Correlation id for the request being served on this task/thread.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("app.access")


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record, so any format can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log shipper that parses rather than greps."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Install a root handler. Idempotent, so repeated calls in tests are free."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, "_fae_configured", False):
            existing.setLevel(level)
            root.setLevel(level)
            return

    handler = logging.StreamHandler()
    handler._fae_configured = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdFilter())
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
            )
        )
    handler.setLevel(level)
    root.addHandler(handler)
    root.setLevel(level)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Stamp every request with an id, log its outcome, echo the id back.

    An inbound ``X-Request-ID`` is honoured so a trace started at a proxy or in
    the frontend carries through, and generated when absent.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "%s %s failed after %dms",
                request.method,
                request.url.path,
                duration_ms,
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %d in %dms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
