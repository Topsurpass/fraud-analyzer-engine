"""Environment-driven settings. Every value has a local-dev default."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings are overridable via ``FAE_``-prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="FAE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App-state database (this service's own storage, not a target DB).
    app_db_url: str = "sqlite:///./fraud_analyzer.db"

    # Fernet key for credential encryption. Generated into .secrets/ if absent.
    fernet_key: str | None = None

    # Row caps.
    default_row_limit: int = Field(default=1000, gt=0)
    max_row_limit: int = Field(default=10000, gt=0)
    preview_row_limit: int = Field(default=100, gt=0)

    # Execution.
    query_timeout_s: int = Field(default=10, gt=0)
    connect_timeout_s: int = Field(default=10, gt=0)

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
    def cors_origin_list(self) -> list[str]:
        """Split the comma-separated origins into a list uvicorn/CORS can use."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def query_timeout_ms(self) -> int:
        return self.query_timeout_s * 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
