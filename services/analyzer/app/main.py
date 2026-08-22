"""FastAPI application: middleware, exception handlers, and router wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import target_registry
from app.db.migrate import bootstrap_schema
from app.errors import AppError, ErrorCode
from app.routers import connections, introspection, queries

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Make the app-state schema usable before serving, clean up after."""
    bootstrap_schema()
    yield
    target_registry.dispose_all()


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


app.include_router(connections.router)
app.include_router(introspection.router)
app.include_router(queries.connection_scoped)
app.include_router(queries.query_scoped)
