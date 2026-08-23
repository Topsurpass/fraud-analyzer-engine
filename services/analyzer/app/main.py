"""FastAPI application: middleware, exception handlers, and router wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import target_registry
from app.db.app_state import get_engine
from app.db.migrate import bootstrap_schema
from app.errors import HTTP_STATUS_BY_CODE, AppError, ErrorCode
from app.observability import RequestContextMiddleware, configure_logging
from app.ratelimit import RateLimitMiddleware, RequestSizeLimitMiddleware
from app.routers import connections, dashboards, introspection, queries

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Make the app-state schema usable before serving, clean up after."""
    configure_logging()
    bootstrap_schema()
    _prune_logs_on_startup()
    yield
    target_registry.dispose_all()


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


app.include_router(connections.router)
app.include_router(dashboards.router)
app.include_router(introspection.router)
app.include_router(queries.connection_scoped)
app.include_router(queries.query_scoped)


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
