"""Environment-driven settings. Every value has a local-dev default."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    max_row_limit: int = Field(default=10000, gt=0)
    preview_row_limit: int = Field(default=100, gt=0)

    # Serverless Postgres suspends when idle, and the first connection after
    # that pays a cold start that can run past ten seconds. This is separate
    # from connect_timeout_s, which bounds connections to *target* databases
    # where a slow connect means something is wrong.
    app_db_connect_timeout_s: int = Field(default=30, gt=0)

    # Execution.
    query_timeout_s: int = Field(default=10, gt=0)
    connect_timeout_s: int = Field(default=10, gt=0)

    # The client socket must outlast the server-side statement timeout. If they
    # fire together the socket usually wins the race, and a query that merely
    # ran long is reported as a lost connection (502) instead of a timeout
    # (504) -- which tells the frontend the database is down when it is not.
    socket_timeout_grace_s: int = Field(default=5, gt=0)

    # Polling.
    poll_interval_ms: int = Field(default=5000, gt=0)

    # HTTP.
    cors_origins: str = "*"

    # Target-engine pooling.
    target_pool_size: int = Field(default=5, gt=0)
    target_max_overflow: int = Field(default=2, ge=0)
    max_target_engines: int = Field(default=32, gt=0)

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
    def query_timeout_ms(self) -> int:
        return self.query_timeout_s * 1000

    @property
    def socket_read_timeout_s(self) -> int:
        """Client socket deadline: always later than the server's own timeout."""
        return self.query_timeout_s + self.socket_timeout_grace_s


@lru_cache
def get_settings() -> Settings:
    return Settings()
