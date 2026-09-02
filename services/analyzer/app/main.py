"""FastAPI application: middleware, exception handlers, and router wiring."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import target_registry
from app.services import refresher, scheduler
from app.db.app_state import get_engine
from app.db.migrate import bootstrap_schema
from app.errors import HTTP_STATUS_BY_CODE, AppError, ErrorCode
from app.observability import RequestContextMiddleware, configure_logging
from app.ratelimit import RateLimitMiddleware, RequestSizeLimitMiddleware
from app.routers import auth, connections, dashboards, flag_rules, introspection, queries, users

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Make the app-state schema usable before serving, clean up after."""
    configure_logging()
    bootstrap_schema()
    _prune_logs_on_startup()
    _report_unreadable_credentials()

    stop = asyncio.Event()
    task = None
    if get_settings().scheduler_enabled:
        task = asyncio.create_task(scheduler.run_forever(stop))
    else:
        logger.info("Flag scheduler disabled by FAE_SCHEDULER_ENABLED=false.")

    yield

    if task is not None:
        stop.set()
        # Bounded: a shutdown must not hang on a query that is mid-flight
        # against a slow target.
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.CancelledError):  # pragma: no cover
            task.cancel()
    refresher.shutdown()
    target_registry.dispose_all()


def _report_unreadable_credentials() -> None:
    """Say at startup which stored credentials this key cannot read.

    A Fernet key that changed between the save and the read makes every query
    on that connection fail, and the failure surfaces one request at a time, on
    a connection whose every visible field is correct. It is the same
    confusion each time, so it belongs in the log at boot rather than being
    rediscovered.

    The key's fingerprint goes in the same line: comparing it across two
    restarts is what turns "the password broke again" into "these are two
    different keys". It is a truncated hash of the key, never the key.
    """
    import hashlib

    from sqlalchemy.orm import Session

    from app.db.app_state import get_engine
    from app.models import Connection
    from app.security.crypto import decrypt

    try:
        settings = get_settings()
        key = settings.fernet_key or ""
        fingerprint = (
            hashlib.sha256(key.encode()).hexdigest()[:12] if key else "generated"
        )

        with Session(get_engine()) as session:
            unreadable = []
            for conn in session.query(Connection).all():
                if not conn.password_encrypted:
                    continue
                try:
                    decrypt(conn.password_encrypted)
                except Exception:  # noqa: BLE001 - any failure means unreadable
                    unreadable.append(conn.name)

        if unreadable:
            logger.warning(
                "Credential encryption key fingerprint %s cannot decrypt the stored "
                "password for: %s. These were saved under a different "
                "FAE_FERNET_KEY, so every query on them will fail until the "
                "password is entered again. Note that a FAE_FERNET_KEY exported in "
                "the shell overrides the one in .env, so starting the stack from "
                "different shells produces different keys.",
                fingerprint,
                ", ".join(sorted(unreadable)),
            )
        else:
            logger.info("Credential encryption key fingerprint %s.", fingerprint)
    except Exception:  # noqa: BLE001 - never block startup on a diagnostic
        logger.exception("Could not check stored credentials at startup; continuing.")


def _prune_logs_on_startup() -> None:
    """Drop execution logs past the retention window.

    Nothing else in the service ever deleted them, and one card polling at the
    default interval writes roughly 17k rows a day on cache misses. On a
    metered backend that is storage that only grows. Failure here is logged and
    swallowed: an unprunable log table is a housekeeping problem, not a reason
    to refuse to serve.
    """
    from app.services import saved_query_service

    try:
        removed = saved_query_service.prune_execution_logs()
        if removed:
            logger.info("Pruned %d execution log rows past the retention window.", removed)
    except Exception:  # noqa: BLE001 - never block startup on housekeeping
        logger.exception("Execution-log pruning failed at startup; continuing.")


app = FastAPI(
    lifespan=lifespan,
    title="Fraud Analyzer Engine",
    version="0.1.0",
    description=(
        "Schema-agnostic backend for saved, read-only SQL exposed as "
        "chart-ready JSON. All fraud logic lives in the SQL you save."
    ),
    # Every response this service returns is machine-read JSON, and the
    # row-carrying ones are large: a 25,000-row by 9-column result measured
    # 2.64 MB. The stdlib encoder was 26 ms of that on its own, and FastAPI's
    # jsonable_encoder another 149 ms before it. orjson serialises the same
    # payload in a fraction of the time and handles datetime and Decimal
    # natively, which is most of what jsonable_encoder was being paid for.
)

_settings = get_settings()

# Order matters and is the reverse of execution order: the last middleware
# added is the outermost. Request context is outermost so a rate-limited 429
# still carries a request id and still gets logged.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After"],
)
# Compression, innermost of the response-shaping middlewares so it sees the
# finished body.
#
# This is the single largest win available at the scale this service is aimed
# at. A 25,000-row result is 2.64 MB of JSON, and JSON of this shape - the same
# nine keys repeated 25,000 times - compresses about tenfold. Uncompressed,
# one card costs a analyst on a 20 Mbps link over a second of pure transfer,
# and a board of eight cards costs 21 MB. That is not a server-time problem, so
# no amount of query tuning shows up against it.
#
# minimum_size skips the compression handshake on the small responses that make
# up most of the request count (auth, listings, unchanged polls), where the CPU
# would cost more than the bytes saved.
#
# Level 1, not the library default of 9. Measured on a real 25,000-row payload
# (bench/gzip_levels.py), where the whole point is that CPU is the contended
# resource once several analysts are watching at once and bytes are not:
#
#   level   size    ratio   compress   transfer@20Mb   total
#   none    2.87M    1.0        0 ms        1146 ms    1146 ms
#   1       0.64M    4.5       19 ms         256 ms     275 ms
#   5       0.51M    5.6       37 ms         205 ms     242 ms
#   9       0.48M    6.0      347 ms         191 ms     538 ms
#
# Level 9 is worse end to end than level 1 despite being smaller. Level 5 wins
# by 33 ms of wall clock and costs twice the CPU per response, which is the
# wrong trade for a server serving many analysts rather than one. Level 1 turns
# 1146 ms of transfer into 275 ms and leaves the CPU for other people's polls.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(AppError)
async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_response())


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Give Pydantic failures the same envelope as every other error."""
    return JSONResponse(
        status_code=422,
        content={
            "error_code": ErrorCode.REQUEST_VALIDATION_ERROR.value,
            "message": "The request body or parameters failed validation.",
            "detail": {"errors": _jsonable_errors(exc)},
        },
    )


def _jsonable_errors(exc: RequestValidationError) -> list[dict]:
    cleaned = []
    for error in exc.errors():
        cleaned.append(
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return cleaned


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    """Never leak a traceback or a driver string to the client on a 500."""
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": ErrorCode.INTERNAL_ERROR.value,
            "message": "An unexpected internal error occurred.",
            "detail": None,
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness. Answers as long as the process is running.

    Deliberately checks nothing: a liveness probe that fails on a database
    outage causes the orchestrator to kill and reschedule a process that was
    working correctly, turning a dependency outage into a restart loop.
    Readiness is what /ready is for.
    """
    return {"status": "ok"}


@app.get("/ready", tags=["meta"])
def ready() -> JSONResponse:
    """Readiness: can this instance actually serve a request?

    /health used to be the only probe and returned ok unconditionally without
    ever touching a database, so an instance whose app-state backend was
    unreachable reported healthy while every real request returned a 500.
    """
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
        logger.warning("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=HTTP_STATUS_BY_CODE[ErrorCode.SERVICE_NOT_READY],
            content={
                "error_code": ErrorCode.SERVICE_NOT_READY.value,
                "message": "The app-state database is not reachable.",
                "detail": None,
            },
        )
    return JSONResponse(status_code=200, content={"status": "ready"})


app.include_router(auth.router)
app.include_router(connections.router)
app.include_router(dashboards.router)
app.include_router(introspection.router)
app.include_router(queries.connection_scoped)
app.include_router(queries.query_scoped)
app.include_router(flag_rules.connection_scoped)
app.include_router(flag_rules.query_scoped)
app.include_router(flag_rules.summary_scoped)
app.include_router(users.router)
app.include_router(users.audit_router)


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------

#: Statuses documented on every operation, with the envelope this service
#: actually returns. FastAPI's generated schema declared only 200 and a 422
#: shaped as {"detail": [...]}, which is the framework default and not what
#: any handler above emits. A client generated from that schema got the wrong
#: type for every 4xx and 5xx, which is why the frontend hand-declares its own
#: error type instead of using the generated one.
_ERROR_STATUSES = sorted({str(status) for status in HTTP_STATUS_BY_CODE.values()})

_ERROR_SCHEMA: dict[str, Any] = {
    "title": "ApiError",
    "type": "object",
    "required": ["error_code", "message"],
    "properties": {
        "error_code": {
            "title": "Error Code",
            "type": "string",
            "enum": [code.value for code in ErrorCode],
            "description": "Machine-readable code. Branch on this, not on the message.",
        },
        "message": {"title": "Message", "type": "string"},
        "detail": {
            "title": "Detail",
            "anyOf": [{"type": "object"}, {"type": "null"}],
            "description": "Structured context for the error, or null.",
        },
    },
}


def custom_openapi() -> dict[str, Any]:
    """Attach the real error envelope to every operation, once, and cache it."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("schemas", {})["ApiError"] = (
        _ERROR_SCHEMA
    )

    error_response = {
        "description": "Structured error. Branch on `error_code`.",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ApiError"}}
        },
    }

    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            responses = operation["responses"]
            for status in _ERROR_STATUSES:
                responses[status] = dict(error_response)

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
