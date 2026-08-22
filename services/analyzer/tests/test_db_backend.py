"""The FAE_DB_BACKEND switch and app-state URL resolution."""

from __future__ import annotations

import pytest

from app.config import DbBackend, Settings, normalize_pg_url

NEON = (
    "postgresql://neondb_owner:pw@ep-x-pooler.us-east-1.aws.neon.tech/"
    "fraud-analyzer-db?channel_binding=require&sslmode=require"
)


@pytest.fixture(autouse=True)
def _no_harness_override(monkeypatch):
    """Drop the test harness's own app-DB override.

    conftest points every test at a temp SQLite file via FAE_APP_DB_URL, which
    is the explicit escape hatch and correctly outranks FAE_DB_BACKEND. These
    tests are about the switch itself, so the override has to go.
    """
    for name in ("FAE_APP_DB_URL", "DATABASE_URL", "FAE_DATABASE_URL",
                 "FAE_DB_BACKEND", "FAE_SQLITE_APP_DB_PATH"):
        monkeypatch.delenv(name, raising=False)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


# ---------------------------------------------------------------------------
# Scheme normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        # already explicit: left alone
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("sqlite:///./x.db", "sqlite:///./x.db"),
        ("mysql+pymysql://u:p@h/d", "mysql+pymysql://u:p@h/d"),
    ],
)
def test_normalize_pg_url(given, expected):
    assert normalize_pg_url(given) == expected


def test_neon_url_keeps_its_query_string():
    # Neon refuses the connection without sslmode, so the rewrite must not
    # touch anything after the scheme.
    out = normalize_pg_url(NEON)
    assert out.startswith("postgresql+psycopg://")
    assert "sslmode=require" in out
    assert "channel_binding=require" in out


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def test_default_backend_is_sqlite():
    s = _settings()
    assert s.db_backend == DbBackend.SQLITE
    assert s.resolved_app_db_url == "sqlite:///./fraud_analyzer.db"
    assert s.app_db_is_sqlite is True


def test_sqlite_path_is_configurable():
    s = _settings(sqlite_app_db_path="/var/data/fae.db")
    assert s.resolved_app_db_url == "sqlite:////var/data/fae.db"


def test_neon_backend_uses_database_url():
    s = _settings(db_backend="neon", database_url=NEON)
    assert s.resolved_app_db_url.startswith("postgresql+psycopg://")
    assert s.app_db_is_sqlite is False


def test_neon_backend_without_database_url_fails_loudly():
    """Silently falling back to SQLite would write saved queries somewhere
    nobody would think to look."""
    with pytest.raises(ValueError, match="requires DATABASE_URL"):
        _settings(db_backend="neon").resolved_app_db_url


def test_database_url_is_ignored_when_backend_is_sqlite():
    s = _settings(db_backend="sqlite", database_url=NEON)
    assert s.resolved_app_db_url.startswith("sqlite:")


def test_explicit_app_db_url_overrides_the_backend():
    s = _settings(db_backend="neon", database_url=NEON, app_db_url="sqlite:///./o.db")
    assert s.resolved_app_db_url == "sqlite:///./o.db"


def test_explicit_app_db_url_is_also_normalised():
    s = _settings(app_db_url="postgresql://u:p@h/d")
    assert s.resolved_app_db_url == "postgresql+psycopg://u:p@h/d"


def test_backend_reads_from_the_environment(monkeypatch):
    monkeypatch.setenv("FAE_DB_BACKEND", "neon")
    monkeypatch.setenv("DATABASE_URL", NEON)
    s = Settings(_env_file=None)
    assert s.db_backend == DbBackend.NEON
    assert s.resolved_app_db_url.startswith("postgresql+psycopg://")


def test_bare_database_url_env_name_is_honoured(monkeypatch):
    """Managed hosts inject DATABASE_URL, not FAE_DATABASE_URL."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    assert Settings(_env_file=None).database_url == "postgresql://u:p@h/d"


def test_prefixed_database_url_also_works(monkeypatch):
    monkeypatch.setenv("FAE_DATABASE_URL", "postgresql://u:p@h/d")
    assert Settings(_env_file=None).database_url == "postgresql://u:p@h/d"


def test_unknown_backend_rejected():
    with pytest.raises(ValueError):
        _settings(db_backend="mongodb")


def test_app_db_connect_timeout_is_generous_for_a_cold_start():
    """Serverless Postgres suspends when idle; the first connect can exceed
    the ten seconds we allow a target database."""
    s = _settings()
    assert s.app_db_connect_timeout_s >= 30
    assert s.app_db_connect_timeout_s > s.connect_timeout_s
