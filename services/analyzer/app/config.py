"""Environment-driven settings. Every value has a local-dev default."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class DbBackend(StrEnum):
    """Which store holds connections, saved queries, and execution logs."""

    SQLITE = "sqlite"
    NEON = "neon"


#: Neon (and most managed Postgres) hand out a bare ``postgresql://`` URL.
#: SQLAlchemy needs the driver spelled out or it reaches for psycopg2, which is
#: not installed. Rewriting here means a URL can be pasted in unedited.
_PG_SCHEME_REWRITES = {
    "postgres://": "postgresql+psycopg://",
    "postgresql://": "postgresql+psycopg://",
}


def normalize_pg_url(url: str) -> str:
    """Point a Postgres URL at psycopg3, leaving everything else untouched."""
    for prefix, replacement in _PG_SCHEME_REWRITES.items():
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    """All settings are overridable via ``FAE_``-prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="FAE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App-state database (this service's own storage, not a target DB) ---
    #
    # ``db_backend`` is the switch. ``app_db_url`` is an escape hatch that
    # overrides it entirely, which is how the test suite points each test at
    # its own throwaway database.
    db_backend: DbBackend = DbBackend.SQLITE
    sqlite_app_db_path: str = "./fraud_analyzer.db"
    app_db_url: str | None = None

    # Run 'alembic upgrade head' at startup. On by default because a container
    # deploy has no shell step between image build and server start, and a
    # service that cannot create its own schema returns "no such table" on
    # every request. Turn it off if you run migrations as a release step, or if
    # several instances start at once and you do not want them racing.
    auto_migrate: bool = True

    # Read from DATABASE_URL, the name every managed Postgres host uses, and
    # from FAE_DATABASE_URL for consistency with the rest of these settings.
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FAE_DATABASE_URL", "DATABASE_URL"),
    )

    # Fernet key for credential encryption. Generated into .secrets/ if absent.
    fernet_key: str | None = None

    # Row caps.
    default_row_limit: int = Field(default=1000, gt=0)
    # 25,000 is the working figure for a monitoring board, so the ceiling has
    # to clear it rather than sit on it. It used to be 10,000, which made the
    # stated workload simply unavailable: a query asking for 25,000 rows was
    # rejected outright with ROW_LIMIT_EXCEEDED. max_result_bytes below is the
    # bound that actually protects memory, and it is unchanged - a row limit
    # says nothing about how wide a row is.
    max_row_limit: int = Field(default=50000, gt=0)
    preview_row_limit: int = Field(default=100, gt=0)

    # Hard ceiling on a single statement's length, in characters.
    #
    # sqlparse is pure Python and its grouping cost is superlinear in token
    # density, not in length. Measured end-to-end through validate_select with
    # token-dense input ("SELECT a,a,a,...,1"), all of it spent before any
    # database is involved and therefore never bounded by the statement
    # timeout:
    #
    #     1,600,000 chars -> 51.2 s   (the original unbounded case)
    #         8,008 chars ->  6.7 s   <- the peak
    #         6,008 chars ->  4.3 s
    #         4,008 chars ->  2.4 s
    #         2,008 chars ->  1.6 s
    #
    # The peak is at roughly 8-9k characters, not at the top end: past ~10k
    # such input trips sqlparse's own 10,000-token ceiling and bails early
    # (10,008 chars -> 1.4 s). So raising this above ~10,000 costs nothing and
    # lowering it below ~8,000 is the only way to cut the peak.
    #
    # 8,000 is the compromise. A deliberately wide but realistic query (200
    # aggregate expressions) is 8,405 characters, so this is at the top of what
    # real analytics SQL needs; ordinary saved queries are a few hundred.
    # Lower it if you want a tighter CPU bound and your queries are short.
    #
    # Three things bound this, and they are meant to be read together: this
    # cap bounds one request, validate_select's cache means a *saved* query
    # pays it once rather than on every poll, and
    # rate_limit_execution_per_minute bounds a flood of distinct statements.
    max_sql_length: int = Field(default=8_000, gt=0)

    # How many distinct validated statements to remember. Saved queries send
    # byte-identical SQL on every run and every cache-missing poll, and
    # revalidating it each time was pure repeated cost on the hot path.
    sql_validation_cache_size: int = Field(default=512, ge=0)

    # Hard ceiling on one result payload, in bytes, measured while rows are
    # coerced. row_limit bounds the number of rows but says nothing about their
    # width, so a single SELECT repeat('x', 1000000000) would otherwise be
    # materialised, hashed, cached, and serialised in full.
    max_result_bytes: int = Field(default=32 * 1024 * 1024, gt=0)

    # Directories a sqlite *target* connection may point into, comma-separated.
    # Without this a connection profile is an arbitrary-file-read primitive:
    # the path is resolved inside the API process, so pointing it at the
    # service's own app-state database dumps every stored credential through
    # the public query endpoints. The app-state file is always refused
    # regardless of what this allows. Empty string means "refuse every sqlite
    # target", which is the right setting for a container deployment.
    sqlite_allowed_dirs: str = "."

    # Serverless Postgres suspends when idle, and the first connection after
    # that pays a cold start that can run past ten seconds. This is separate
    # from connect_timeout_s, which bounds connections to *target* databases
    # where a slow connect means something is wrong.
    app_db_connect_timeout_s: int = Field(default=30, gt=0)

    # Execution.
    query_timeout_s: int = Field(default=10, gt=0)

    # Serverless Postgres suspends when idle, and the first connection after
    # that pays a cold start while the compute wakes. Measured against a
    # suspended Neon instance: TCP is accepted in 0.27s and the *Postgres*
    # handshake is what waits, so a 10s budget expired and the card reported a
    # timeout on a database that was merely asleep.
    #
    # Read together with target_max_pinned_addresses below: the timeout is per
    # address, so the worst case for a genuinely dead host is the product of
    # the two. 15 x 2 keeps that at the 30s it already was while giving a cold
    # start half again as long to answer.
    connect_timeout_s: int = Field(default=15, gt=0)

    # How many resolved addresses to hand libpq for one target.
    #
    # More is not better. Failover wants a second address; a third only adds
    # another connect_timeout to the wait before an unreachable host is
    # reported, and managed Postgres publishes several addresses for the same
    # proxy rather than several independent endpoints.
    target_max_pinned_addresses: int = Field(default=2, gt=0)

    # CA bundle used by the verifying TLS modes when a connection names no
    # certificate of its own. Empty means "find the system bundle", which is
    # what a public CA (Neon, RDS, Supabase) needs. Set this when every target
    # sits behind one internal CA, rather than repeating the path per
    # connection. Deliberately not "system": libpq accepts that spelling but it
    # resolves to OpenSSL's compiled-in directory, which is not where the
    # bundle lives in this image.
    target_ssl_root_cert: str = Field(default="")

    # The client socket must outlast the server-side statement timeout. If they
    # fire together the socket usually wins the race, and a query that merely
    # ran long is reported as a lost connection (502) instead of a timeout
    # (504) -- which tells the frontend the database is down when it is not.
    socket_timeout_grace_s: int = Field(default=5, gt=0)

    # Scheduler. The only part of the service that queries a customer's
    # database unprompted, so it is bounded and can be switched off entirely.
    scheduler_enabled: bool = True
    #: How often the loop looks for due queries. Not how often a query runs.
    scheduler_tick_ms: int = Field(default=15_000, gt=0)
    #: Floor under every query's own interval, so a query saved with a
    #: one-second interval cannot become a denial of service by a typo.
    scheduler_min_interval_ms: int = Field(default=60_000, gt=0)
    #: Ceiling on the backoff a repeatedly failing target reaches.
    scheduler_max_backoff_ms: int = Field(default=3_600_000, gt=0)

    # How long past its TTL a cached result may still be served while a fresh
    # one is fetched behind it.
    #
    # This is what stops a poll blocking on the target database. With a 5s TTL
    # and a 2.4s query, half of all polls used to run the query inline and the
    # card sat empty while they did. Past this window the entry is dropped and
    # the next poll runs the query for real: stale data is better than no data,
    # but "an hour ago" is not an answer to "what is happening now".
    cache_stale_grace_ms: int = Field(default=300_000, gt=0)

    # How far a measure has to move, in percent, before a chart calls it out
    # for investigation. A magnitude: 50 covers a 50% rise and a 50% fall.
    #
    # A percentage rather than an absolute figure because terminals do not
    # carry comparable volume - one does twenty times another's, so an
    # absolute jump that is alarming on a quiet terminal is noise on a busy
    # one. Charts store their own value and fall back to this when unset.
    default_surge_threshold_pct: float = Field(default=50.0, gt=0, le=100_000)

    # Polling.
    poll_interval_ms: int = Field(default=5000, gt=0)

    # Approximate bytes the poll result cache may hold in total. The cache used
    # to be bounded by entry count alone, which said nothing about cost: one
    # 10,000-row by 5-column result measured 1.1 MB, so 256 entries was really
    # a 0.27 GB ceiling for narrow results and about 1 GB for wide ones. A
    # small container is OOM-killed long before eviction triggers.
    cache_max_bytes: int = Field(default=64 * 1024 * 1024, gt=0)

    # Bytes the rendered-response cache may hold: poll payloads already encoded
    # to JSON and gzipped, so they can be written to a socket without being
    # rebuilt per viewer. Smaller than cache_max_bytes on purpose. Entries here
    # are compressed, about a fifth the size of the same result sitting in the
    # result cache as a Python dict, and this cache only ever holds results
    # somebody is actively polling.
    rendered_cache_max_bytes: int = Field(default=32 * 1024 * 1024, gt=0)

    # Most queries one "refresh flagged" click may re-run against a target
    # database. The flagged view itself reads cache and runs nothing; refresh
    # is the only path that executes, and without a bound one click on a
    # connection with fifty saved queries is fifty statements at once against a
    # pool of 10 + 5. Queries past the cap keep their cached rows and the
    # response says it truncated.
    flagged_refresh_max_queries: int = Field(default=20, gt=0)

    # HTTP.
    cors_origins: str = "*"

    # Requests per minute per client IP, 0 to disable. There is no auth, so
    # every execution endpoint runs caller-supplied SQL against a customer
    # production database; a bucket is the only thing bounding that. The
    # execution bucket has to absorb a legitimate dashboard: twelve cards at a
    # five-second interval is 144 polls/minute from one browser.
    rate_limit_per_minute: int = Field(default=600, ge=0)

    # Largest request body accepted, in bytes, checked against Content-Length
    # before the body is read. The SQL guard caps statement length, but only
    # after Starlette has buffered and decoded the whole payload. 0 disables.
    max_request_bytes: int = Field(default=1024 * 1024, ge=0)
    rate_limit_execution_per_minute: int = Field(default=300, ge=0)

    # Logging. Nothing configures the root logger otherwise, so uvicorn's
    # default leaves root at WARNING and every INFO line the service emits --
    # including the startup banner the README tells operators to check -- is
    # silently discarded.
    log_level: str = "INFO"
    log_json: bool = False

    # Execution-log retention. One card polling at the default interval writes
    # roughly 17k rows a day on cache misses, forever, and nothing else in the
    # service ever deletes them. 0 disables pruning.
    log_retention_days: int = Field(default=30, ge=0)
    max_logs_per_query: int = Field(default=1000, ge=0)

    # Target-engine pooling.
    target_pool_size: int = Field(default=10, gt=0)
    target_max_overflow: int = Field(default=5, ge=0)
    max_target_engines: int = Field(default=32, gt=0)

    # How long a request waits for a pooled connection before giving up.
    # SQLAlchemy's default is 30 s, which outlives the frontend's poll deadline
    # and turns pool exhaustion into a hang rather than an error.
    target_pool_timeout_s: int = Field(default=5, gt=0)


    # Sessions.
    #
    # Absolute lifetime is not extended by use: a session that has existed for
    # twelve hours ends whether or not somebody is still typing, which bounds
    # how long a stolen cookie is worth anything.
    session_absolute_hours: int = Field(default=12, gt=0)
    #: Idle timeout. Shorter than the absolute lifetime, and refreshed on use.
    session_idle_hours: int = Field(default=8, gt=0)

    @field_validator("max_row_limit")
    @classmethod
    def _ceiling_at_least_default(cls, v: int, info) -> int:
        default = info.data.get("default_row_limit")
        if default is not None and v < default:
            raise ValueError("max_row_limit must be >= default_row_limit")
        return v

    @property
    def resolved_app_db_url(self) -> str:
        """The URL the app-state engine actually connects to.

        Order: an explicit ``FAE_APP_DB_URL`` wins, then the selected backend.
        Selecting ``neon`` without a ``DATABASE_URL`` is a configuration error
        worth failing loudly on, rather than silently writing saved queries to
        a local SQLite file nobody will think to look in.
        """
        if self.app_db_url:
            return normalize_pg_url(self.app_db_url)

        if self.db_backend == DbBackend.NEON:
            if not self.database_url:
                raise ValueError(
                    "FAE_DB_BACKEND=neon requires DATABASE_URL to be set. "
                    "Set it in .env, or switch to FAE_DB_BACKEND=sqlite."
                )
            return normalize_pg_url(self.database_url)

        return f"sqlite:///{self.sqlite_app_db_path}"

    @property
    def app_db_is_sqlite(self) -> bool:
        return self.resolved_app_db_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        """Split the comma-separated origins into a list uvicorn/CORS can use."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sqlite_allowed_dir_list(self) -> list[Path]:
        """Absolute, symlink-resolved roots a sqlite target may live under.

        Resolved rather than merely absolute so a path cannot walk out through
        ``..`` or a symlink and still compare as inside an allowed root.
        """
        return [
            Path(part.strip()).expanduser().resolve()
            for part in self.sqlite_allowed_dirs.split(",")
            if part.strip()
        ]

    @property
    def app_db_sqlite_file(self) -> Path | None:
        """Absolute path of the app-state SQLite file, if that is the backend.

        Used to refuse a target connection that points at this service's own
        database, which would otherwise expose every stored credential
        ciphertext through the ordinary query endpoints.
        """
        url = self.resolved_app_db_url
        if not url.startswith("sqlite"):
            return None
        database = make_url(url).database
        if not database or database == ":memory:":
            return None
        return Path(database).expanduser().resolve()

    @property
    def query_timeout_ms(self) -> int:
        return self.query_timeout_s * 1000

    @property
    def socket_read_timeout_s(self) -> int:
        """Client socket deadline: always later than the server's own timeout."""
        return self.query_timeout_s + self.socket_timeout_grace_s


@lru_cache
def get_settings() -> Settings:
    return Settings()
