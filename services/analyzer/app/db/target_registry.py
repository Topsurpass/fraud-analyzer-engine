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
import re
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
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import QueuePool

from app.config import get_settings
from app.errors import (
    AppError,
    DbAuthError,
    DbTlsRequiredError,
    DbPermissionError,
    DbUnreachableError,
    InvalidConfigError,
    QueryExecutionError,
    QueryTimeoutError,
)
from app.models import Connection, DbType
from app.models.enums import VERIFYING_SSL_MODES, SslMode
from app.security.crypto import decrypt
from app.security.sqlite_paths import resolve_sqlite_path

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


#: The "- host: 'x', port: N, hostaddr: 'a':" preamble psycopg puts on each
#: attempt. Stripped before deduplicating, or the differing address makes two
#: identical failures look like two different ones.
_ATTEMPT_PREFIX = re.compile(r"^-\s*host:.*?hostaddr:\s*'[^']*':\s*")

#: Quoted host addresses, masked out of the deduplication key only.
_QUOTED = re.compile(r'"[^"]*"')

#: psycopg's banner when a host resolved to more than one address. Everything
#: before it is a repeat of the *last* attempt to fail.
_MULTI_ATTEMPT_BANNER = "Multiple connection attempts failed"


def _clean(orig: BaseException) -> str:
    """Collapse a driver message to one line, so it fits a JSON error body.

    Managed Postgres resolves to several addresses, and psycopg tries each one
    and then reports the *last* failure as the headline followed by the full
    list. When the host has an AAAA record and the container has no IPv6 route,
    that headline is always "Network is unreachable" for the IPv6 address -
    while the IPv4 attempts, the ones that actually reached the server, carry
    the real reason further down. Truncating from the front kept the noise and
    dropped the answer, so the per-attempt lines are hoisted ahead of it.
    """
    text_ = str(orig)
    if _MULTI_ATTEMPT_BANNER in text_:
        _, _, listing = text_.partition(_MULTI_ATTEMPT_BANNER)
        attempts = [
            " ".join(line.split())
            for line in listing.splitlines()
            if line.strip().startswith("-")
        ]
        if attempts:
            # Deduplicated by reason, not by line: several addresses of the same
            # host usually fail identically, and three copies of one sentence
            # crowd out the one attempt that differs. The address has to come
            # off the front first or every line looks unique.
            seen: list[str] = []
            keys: set[str] = set()
            for attempt in attempts:
                reason = _ATTEMPT_PREFIX.sub("", attempt, count=1)
                # Each reason quotes its own address, so two identical failures
                # differ by that alone. Masking it for the key collapses them
                # while the text kept is still the real, unedited first one.
                key = _QUOTED.sub('"?"', reason)
                if key not in keys:
                    keys.add(key)
                    seen.append(reason)
            text_ = " ".join(seen)
    return " ".join(text_.split())[:500] or type(orig).__name__


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

    # Pool exhaustion, checked before anything else because it is not a
    # database error at all: the query never ran. It used to fall through to
    # the text match on "timed out" below and surface as QUERY_TIMEOUT, which
    # told the frontend a query was slow when in fact the service had no free
    # connection. The driver message also spells out the pool configuration
    # ("QueuePool limit of size 5 overflow 2 reached"), so it is replaced
    # rather than passed through.
    if isinstance(exc, PoolTimeout):
        return DbUnreachableError(
            "No connection to the target database was available in time. "
            "Too many queries are running against this connection at once.",
            {"reason": "pool_exhausted"},
        )

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
    # Checked before the timeout probe: a TLS refusal is a configuration
    # problem with one specific fix, and saying so beats any generic mapping.
    if "connection is insecure" in message or "server does not support ssl" in message:
        return DbTlsRequiredError(
            "The target refused an unencrypted connection. Set this "
            "connection's TLS mode to 'require' or stronger.",
            {"reason": "tls_required"},
        )
    if "timeout" in message or "timed out" in message:
        return QueryTimeoutError(_clean(orig))
    if "authentication" in message or "access denied" in message:
        return DbAuthError(_clean(orig))
    if (
        "could not connect" in message
        or "connection refused" in message
        # Reported by libpq when an address family has no route at all, which
        # is the normal case for a AAAA record inside a container with no IPv6.
        or "network is unreachable" in message
        or "no route to host" in message
        or "name or service not known" in message
        or "failed to resolve host" in message
    ):
        return DbUnreachableError(_clean(orig))
    if isinstance(exc, (DBAPIError, SQLAlchemyError)) and getattr(
        exc, "connection_invalidated", False
    ):  # pragma: no cover - needs a mid-query server disconnect
        return DbUnreachableError(_clean(orig))

    return QueryExecutionError(_clean(orig))


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def build_url(conn: Connection) -> URL:
    """Build the SQLAlchemy URL for a connection, decrypting the password."""
    if conn.db_type == DbType.SQLITE:
        return URL.create("sqlite", database=str(resolve_sqlite_path(conn.sqlite_path)))

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
    # Validated here, not only at write time, so a connection row that predates
    # the allowlist is checked when it is used rather than trusted because it
    # is already stored.
    resolved = str(resolve_sqlite_path(path))

    def creator():
        # mode=ro makes the handle itself incapable of writing, which is
        # stronger than relying on the statement never being a write.
        return sqlite3.connect(
            f"file:{resolved}?mode=ro",
            uri=True,
            check_same_thread=False,
        )

    return creator


#: Where a system CA bundle lives, most common first. Only consulted when a
#: verifying mode is in use and neither the connection nor the settings name a
#: certificate.
_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL, Fedora, Amazon Linux
)


def resolve_ca_bundle(conn: Connection) -> str | None:
    """The root certificate a verifying TLS mode should check against.

    Order: the connection's own certificate, then ``FAE_TARGET_SSL_ROOT_CERT``,
    then whichever system bundle exists. Returns ``None`` for the non-verifying
    modes, which must not be handed a certificate at all - passing one would
    imply a check that is not happening.
    """
    if conn.ssl_mode not in VERIFYING_SSL_MODES:
        return None
    if conn.ssl_root_cert:
        return conn.ssl_root_cert
    configured = get_settings().target_ssl_root_cert
    if configured:
        return configured
    for candidate in _CA_BUNDLE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    # Nothing found. Returning None lets libpq fail with its own message about
    # the missing root certificate, which names the path it wanted; inventing
    # one here would produce a worse error at the same point.
    return None


def postgres_connect_args(conn: Connection) -> dict:
    """libpq options for a read-only, time-bounded, TLS-configured session.

    Applied as connection options rather than a per-transaction
    ``SET TRANSACTION READ ONLY``, so no code path can forget them.

    ``sslmode`` is always sent explicitly. libpq's own default is ``prefer``,
    which offers plaintext first and accepts it silently, so leaving it unset
    both breaks targets that require TLS and hides a downgrade on targets that
    do not.
    """
    settings = get_settings()
    timeout_ms = settings.query_timeout_ms
    args = {
        "connect_timeout": settings.connect_timeout_s,
        "options": (
            f"-c default_transaction_read_only=on "
            f"-c statement_timeout={timeout_ms} "
            f"-c idle_in_transaction_session_timeout={timeout_ms}"
        ),
        # libpq speaks these spellings natively, so the enum is the mapping.
        "sslmode": conn.ssl_mode.value,
    }
    bundle = resolve_ca_bundle(conn)
    if bundle:
        args["sslrootcert"] = bundle
    return args


def mysql_connect_args(conn: Connection) -> dict:
    """pymysql socket and TLS options.

    ``read_timeout`` is deliberately longer than the server's
    ``max_execution_time``. If they fire together the socket usually wins the
    race and a merely slow query surfaces as errno 2013 "lost connection",
    which maps to 502 DB_UNREACHABLE and tells the frontend the database is
    down. The server must get the chance to return errno 3024 first.

    pymysql has no ``sslmode``, so :class:`~app.models.enums.SslMode` is mapped
    onto its own flags. The mapping is not one-to-one and cannot be: passing
    *any* ``ssl`` argument makes pymysql insist on TLS, so there is no way to
    express "try TLS, fall back to plaintext". ``disable``, ``allow`` and
    ``prefer`` therefore all mean "send no TLS configuration and let the server
    decide", which is what this driver did before there was a mode at all.
    Anything from ``require`` up is enforced.
    """
    settings = get_settings()
    args = {
        "connect_timeout": settings.connect_timeout_s,
        "read_timeout": settings.socket_read_timeout_s,
        "write_timeout": settings.socket_read_timeout_s,
    }

    if conn.ssl_mode in (SslMode.DISABLE, SslMode.ALLOW, SslMode.PREFER):
        return args

    bundle = resolve_ca_bundle(conn)
    args["ssl"] = {"ca": bundle} if bundle else {}
    # require encrypts without checking who is on the other end; verify-ca
    # checks the certificate; verify-full also checks the hostname matches.
    args["ssl_verify_cert"] = conn.ssl_mode in VERIFYING_SSL_MODES
    args["ssl_verify_identity"] = conn.ssl_mode is SslMode.VERIFY_FULL
    return args


def _create_engine_for(conn: Connection) -> Engine:
    settings = get_settings()
    timeout_ms = settings.query_timeout_ms

    if conn.db_type == DbType.SQLITE:
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
            pool_timeout=settings.target_pool_timeout_s,
        )

    url = build_url(conn)

    if conn.db_type == DbType.POSTGRES:
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_size=settings.target_pool_size,
            max_overflow=settings.target_max_overflow,
            pool_timeout=settings.target_pool_timeout_s,
            pool_recycle=1800,
            connect_args=postgres_connect_args(conn),
        )

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.target_pool_size,
        max_overflow=settings.target_max_overflow,
            pool_timeout=settings.target_pool_timeout_s,
        pool_recycle=1800,
        connect_args=mysql_connect_args(conn),
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
