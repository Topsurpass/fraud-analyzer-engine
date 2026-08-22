"""Pooled, read-only engines for target databases.

One SQLAlchemy engine per :class:`~app.models.Connection`, kept in a bounded
LRU registry so a request never pays for engine construction and a long-lived
process never accumulates unbounded pools.

Read-only is enforced at the driver level, before any SQL is sent:

* **PostgreSQL** connects with ``default_transaction_read_only=on``. Setting it
  as a connection option rather than issuing ``SET TRANSACTION READ ONLY`` per
  transaction means it cannot be forgotten on some code path, and the guard
  already rejects any ``SET`` statement that would try to turn it off.
* **MySQL** issues ``SET SESSION TRANSACTION READ ONLY`` on every new pooled
  connection.
* **SQLite** opens the file with the ``mode=ro`` URI flag, so the handle itself
  cannot write.

This is the second line of defence. The first is :mod:`app.security.sql_guard`,
and the real backstop is a read-only database role, which the README tells
operators to use.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.engine import Connection as SAConnection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import QueuePool

from app.config import get_settings
from app.errors import (
    AppError,
    DbAuthError,
    DbPermissionError,
    DbUnreachableError,
    InvalidConfigError,
    QueryExecutionError,
    QueryTimeoutError,
)
from app.models import Connection, DbType
from app.security.crypto import decrypt

logger = logging.getLogger(__name__)

DEFAULT_PORTS = {DbType.POSTGRES: 5432, DbType.MYSQL: 3306}

_engines: OrderedDict[str, Engine] = OrderedDict()
_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

#: PostgreSQL SQLSTATE -> exception factory. Class 08 is handled by prefix.
_PG_SQLSTATE = {
    "28P01": DbAuthError,
    "28000": DbAuthError,
    "42501": DbPermissionError,
    "57014": QueryTimeoutError,
    "25006": DbPermissionError,  # read-only transaction rejected a write
}

#: MySQL error numbers -> exception factory.
_MYSQL_ERRNO = {
    1045: DbAuthError,
    1044: DbPermissionError,
    1142: DbPermissionError,
    1143: DbPermissionError,
    1290: DbPermissionError,
    1792: DbPermissionError,  # read-only transaction rejected a write
    3024: QueryTimeoutError,
    2003: DbUnreachableError,
    2005: DbUnreachableError,
    2006: DbUnreachableError,
    2013: DbUnreachableError,
    1049: DbUnreachableError,  # unknown database
}


def _clean(orig: BaseException) -> str:
    """Collapse a driver message to one line, so it fits a JSON error body."""
    return " ".join(str(orig).split())[:500] or type(orig).__name__


def _sqlstate_of(orig: BaseException | None) -> str | None:
    for attr in ("sqlstate", "pgcode"):
        value = getattr(orig, attr, None)
        if value:
            return str(value)
    return None


def _mysql_errno_of(orig: BaseException | None) -> int | None:
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _translate_sqlite(orig: BaseException) -> AppError | None:
    message = str(orig).lower()
    if "readonly database" in message or "read-only" in message:
        return DbPermissionError(
            "The target SQLite database is open read-only and rejected a write."
        )
    if "unable to open database file" in message or "no such file" in message:
        return DbUnreachableError(f"Cannot open the SQLite database file: {_clean(orig)}")
    if "interrupted" in message:
        return QueryTimeoutError("The query exceeded the statement timeout.")
    return None


def translate_db_error(exc: BaseException, *, timed_out: bool = False) -> AppError:
    """Map a driver exception onto the API's error taxonomy.

    Mapping is driven by SQLSTATE and MySQL error numbers rather than by
    matching exception text, so a driver changing its wording does not silently
    reclassify an auth failure as a generic query error.
    """
    if isinstance(exc, AppError):
        return exc
    if timed_out:
        return QueryTimeoutError("The query exceeded the statement timeout.")

    orig = getattr(exc, "orig", None) or exc

    sqlstate = _sqlstate_of(orig)
    if sqlstate:
        if sqlstate in _PG_SQLSTATE:
            return _PG_SQLSTATE[sqlstate](_clean(orig), {"sqlstate": sqlstate})
        if sqlstate.startswith("08"):
            return DbUnreachableError(_clean(orig), {"sqlstate": sqlstate})
        if sqlstate.startswith("42"):
            return QueryExecutionError(_clean(orig), {"sqlstate": sqlstate})

    errno = _mysql_errno_of(orig)
    if errno in _MYSQL_ERRNO:
        return _MYSQL_ERRNO[errno](_clean(orig), {"errno": errno})

    if isinstance(orig, sqlite3.Error):
        translated = _translate_sqlite(orig)
        if translated is not None:
            return translated

    message = str(orig).lower()
    if "timeout" in message or "timed out" in message:
        return QueryTimeoutError(_clean(orig))
    if "authentication" in message or "access denied" in message:
        return DbAuthError(_clean(orig))
    if "could not connect" in message or "connection refused" in message:
        return DbUnreachableError(_clean(orig))
    if isinstance(exc, (DBAPIError, SQLAlchemyError)) and getattr(
        exc, "connection_invalidated", False
    ):
        return DbUnreachableError(_clean(orig))

    return QueryExecutionError(_clean(orig))


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def build_url(conn: Connection) -> URL:
    """Build the SQLAlchemy URL for a connection, decrypting the password."""
    if conn.db_type == DbType.SQLITE:
        if not conn.sqlite_path:
            raise InvalidConfigError("A sqlite connection requires 'sqlite_path'.")
        return URL.create("sqlite", database=conn.sqlite_path)

    if not conn.host or not conn.database:
        raise InvalidConfigError(
            f"A {conn.db_type.value} connection requires 'host' and 'database'."
        )

    password = decrypt(conn.password_encrypted) if conn.password_encrypted else None
    driver = "postgresql+psycopg" if conn.db_type == DbType.POSTGRES else "mysql+pymysql"
    return URL.create(
        driver,
        username=conn.username,
        password=password,
        host=conn.host,
        port=conn.port or DEFAULT_PORTS[conn.db_type],
        database=conn.database,
    )


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------


def _sqlite_creator(path: str):
    resolved = str(Path(path).expanduser())

    def creator():
        # mode=ro makes the handle itself incapable of writing, which is
        # stronger than relying on the statement never being a write.
        return sqlite3.connect(
            f"file:{resolved}?mode=ro",
            uri=True,
            check_same_thread=False,
        )

    return creator


def _create_engine_for(conn: Connection) -> Engine:
    settings = get_settings()
    timeout_ms = settings.query_timeout_ms

    if conn.db_type == DbType.SQLITE:
        if not conn.sqlite_path:
            raise InvalidConfigError("A sqlite connection requires 'sqlite_path'.")
        # SQLAlchemy defaults a bare "sqlite://" URL to SingletonThreadPool,
        # which cannot be sized. The creator opens a real file with
        # check_same_thread=False, so a normal QueuePool is both safe and
        # what lets the pool settings apply.
        return create_engine(
            "sqlite://",
            creator=_sqlite_creator(conn.sqlite_path),
            poolclass=QueuePool,
            pool_pre_ping=True,
            pool_size=settings.target_pool_size,
            max_overflow=settings.target_max_overflow,
        )

    url = build_url(conn)

    if conn.db_type == DbType.POSTGRES:
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_size=settings.target_pool_size,
            max_overflow=settings.target_max_overflow,
            pool_recycle=1800,
            connect_args={
                "connect_timeout": settings.connect_timeout_s,
                "options": (
                    f"-c default_transaction_read_only=on "
                    f"-c statement_timeout={timeout_ms} "
                    f"-c idle_in_transaction_session_timeout={timeout_ms}"
                ),
            },
        )

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.target_pool_size,
        max_overflow=settings.target_max_overflow,
        pool_recycle=1800,
        connect_args={
            "connect_timeout": settings.connect_timeout_s,
            "read_timeout": settings.query_timeout_s,
            "write_timeout": settings.query_timeout_s,
        },
    )

    @event.listens_for(engine, "connect")
    def _mysql_read_only(dbapi_connection, _record):  # pragma: no cover - needs MySQL
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET SESSION max_execution_time = {timeout_ms}")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cursor.close()

    return engine


def get_engine(conn: Connection) -> Engine:
    """Return the pooled engine for ``conn``, creating it on first use.

    The registry is an LRU bounded by ``FAE_MAX_TARGET_ENGINES``. Evicting the
    least recently used engine disposes its pool, so idle connections to a
    database nobody queries any more do not linger.
    """
    settings = get_settings()
    with _lock:
        engine = _engines.get(conn.id)
        if engine is not None:
            _engines.move_to_end(conn.id)
            return engine

        engine = _create_engine_for(conn)
        _engines[conn.id] = engine
        _engines.move_to_end(conn.id)

        evicted: list[Engine] = []
        while len(_engines) > settings.max_target_engines:
            evicted_id, evicted_engine = _engines.popitem(last=False)
            logger.info("Evicting pooled engine for connection %s", evicted_id)
            evicted.append(evicted_engine)

    for engine_to_close in evicted:
        engine_to_close.dispose()
    return engine


def dispose_engine(connection_id: str) -> None:
    """Drop the cached engine for a connection. Call after edit or delete."""
    with _lock:
        engine = _engines.pop(connection_id, None)
    if engine is not None:
        engine.dispose()


def dispose_all() -> None:
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


def registry_size() -> int:
    with _lock:
        return len(_engines)


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------


def _install_sqlite_deadline(
    sa_conn: SAConnection, timeout_s: float, state: dict
) -> sqlite3.Connection | None:
    """Abort a SQLite query once it passes its deadline.

    SQLite has no server-side statement timeout, so the only way to bound a
    runaway query is the progress handler: SQLite calls it every N virtual
    machine instructions and aborts when it returns non-zero. The ``state``
    flag records that we were the ones who stopped it, so the failure is
    reported as a timeout rather than as a generic "interrupted" driver error.
    """
    raw = sa_conn.connection.dbapi_connection
    if not isinstance(raw, sqlite3.Connection):  # pragma: no cover - defensive
        return None

    deadline = time.monotonic() + timeout_s

    def _handler() -> int:
        if time.monotonic() > deadline:
            state["timed_out"] = True
            return 1
        return 0

    raw.set_progress_handler(_handler, 1000)
    return raw


@contextmanager
def read_only_connection(
    conn: Connection, timeout_s: float | None = None
) -> Generator[SAConnection, None, None]:
    """Yield a connection that can only read, with a bounded statement time.

    Any driver exception raised inside the block is translated into the API's
    error taxonomy, so callers never have to know which driver they are on.
    """
    timeout_s = timeout_s if timeout_s is not None else get_settings().query_timeout_s
    state = {"timed_out": False}
    raw_sqlite: sqlite3.Connection | None = None

    try:
        engine = get_engine(conn)
        with engine.connect() as sa_conn:
            if conn.db_type == DbType.SQLITE:
                raw_sqlite = _install_sqlite_deadline(sa_conn, timeout_s, state)
            try:
                yield sa_conn
            finally:
                if raw_sqlite is not None:
                    raw_sqlite.set_progress_handler(None, 0)
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - translated into the taxonomy
        raise translate_db_error(exc, timed_out=state["timed_out"]) from exc


def probe(conn: Connection, timeout_s: float | None = None) -> None:
    """Open a connection and run ``SELECT 1``. Raises an ``AppError`` on failure."""
    with read_only_connection(conn, timeout_s=timeout_s) as sa_conn:
        sa_conn.execute(text("SELECT 1")).fetchone()
