"""Engine and session factory for the service's own database.

This is the app-state DB (connection profiles, saved queries, execution logs).
It is entirely separate from the target databases being analysed, which are
handled by :mod:`app.db.target_registry`.
"""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base


@lru_cache
def get_engine() -> Engine:
    """Build the app-state engine for whichever backend is selected.

    Which URL this is comes from ``Settings.resolved_app_db_url``: the
    ``FAE_DB_BACKEND`` switch, or an explicit ``FAE_APP_DB_URL`` override.
    """
    settings = get_settings()
    url = settings.resolved_app_db_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}

    if url.startswith("sqlite"):
        # check_same_thread=False lets FastAPI's threadpool share the engine.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Managed Postgres (Neon and friends) drops idle connections, and a
        # serverless instance may have suspended since the last request.
        # pool_pre_ping already discards dead handles; recycling keeps the pool
        # from holding one long enough to be dropped mid-request.
        kwargs["pool_recycle"] = 300
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 5
        kwargs["connect_args"] = {
            "connect_timeout": settings.app_db_connect_timeout_s
        }

    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":
        # SQLite ignores ON DELETE CASCADE unless foreign keys are enabled, and
        # the pragma is per-connection, so it has to be set on every checkout.
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that always closes."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create every app-state table.

    Alembic owns the schema in production. This exists for tests and for a
    first local run before ``alembic upgrade head``.
    """
    Base.metadata.create_all(bind=get_engine())


def reset_caches() -> None:
    """Drop cached engine/sessionmaker so a test can repoint the app DB."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
