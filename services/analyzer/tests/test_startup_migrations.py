"""The service must be able to stand up its own schema.

Regression for a deployed container returning
``sqlite3.OperationalError: no such table: connections`` on every request:
migrations had never run there, and nothing at startup noticed or said so.
A fresh Postgres would have failed identically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture
def unmigrated_db(tmp_path, monkeypatch):
    """Point the app at a database that exists but has no tables."""
    from app.db import app_state

    monkeypatch.setenv("FAE_APP_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    get_settings.cache_clear()
    app_state.reset_caches()
    yield
    get_settings.cache_clear()
    app_state.reset_caches()


def test_startup_creates_the_schema_when_auto_migrate_is_on(unmigrated_db, monkeypatch):
    monkeypatch.setenv("FAE_AUTO_MIGRATE", "true")
    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/connections")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_startup_reports_a_missing_schema_when_auto_migrate_is_off(
    unmigrated_db, monkeypatch
):
    monkeypatch.setenv("FAE_AUTO_MIGRATE", "false")
    get_settings.cache_clear()

    from app.db.migrate import SchemaNotReadyError, verify_schema

    with pytest.raises(SchemaNotReadyError) as ei:
        verify_schema()
    message = str(ei.value)
    # The message has to tell an operator what to actually do.
    assert "alembic upgrade head" in message
    assert "FAE_AUTO_MIGRATE" in message


def test_migrations_are_idempotent(unmigrated_db, monkeypatch):
    monkeypatch.setenv("FAE_AUTO_MIGRATE", "true")
    get_settings.cache_clear()

    from app.db.migrate import run_migrations, verify_schema

    run_migrations()
    run_migrations()  # second call must be a no-op, not an error
    verify_schema()


def test_verify_schema_passes_once_migrated(unmigrated_db, monkeypatch):
    monkeypatch.setenv("FAE_AUTO_MIGRATE", "true")
    get_settings.cache_clear()

    from app.db.migrate import run_migrations, verify_schema

    run_migrations()
    verify_schema()


def test_auto_migrate_defaults_to_on(monkeypatch):
    """A container deploy should not need a separate release step to boot."""
    from app.config import Settings

    # conftest turns this off for speed; the default itself is what matters.
    monkeypatch.delenv("FAE_AUTO_MIGRATE", raising=False)
    assert Settings(_env_file=None).auto_migrate is True


def test_startup_banner_never_logs_the_password(unmigrated_db, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("FAE_APP_DB_URL", "postgresql://u:sup3rs3cret@h/d")
    monkeypatch.setenv("FAE_AUTO_MIGRATE", "false")
    get_settings.cache_clear()

    from app.db.migrate import describe_app_db

    with caplog.at_level(logging.INFO):
        described = describe_app_db()

    assert "sup3rs3cret" not in described
    assert "sup3rs3cret" not in caplog.text
    assert "postgresql" in described


def test_sqlite_backend_warns_that_data_is_lost_on_restart(unmigrated_db, caplog):
    import logging

    from app.db.migrate import warn_if_storage_is_ephemeral

    with caplog.at_level(logging.WARNING):
        warn_if_storage_is_ephemeral()

    assert "ephemeral" in caplog.text
    assert "FAE_DB_BACKEND=neon" in caplog.text


def test_postgres_backend_does_not_warn(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("FAE_APP_DB_URL", "postgresql://u:p@h/d")
    get_settings.cache_clear()

    from app.db.migrate import warn_if_storage_is_ephemeral

    with caplog.at_level(logging.WARNING):
        warn_if_storage_is_ephemeral()

    assert "ephemeral" not in caplog.text


def test_warns_when_the_encryption_key_was_generated(tmp_path, monkeypatch, caplog):
    """A generated key plus a durable database means unreadable credentials
    after the next restart."""
    import logging

    from app.security import crypto

    monkeypatch.delenv("FAE_FERNET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    crypto.get_fernet.cache_clear()

    from app.db.migrate import warn_if_encryption_key_is_ephemeral

    with caplog.at_level(logging.WARNING):
        warn_if_encryption_key_is_ephemeral()

    assert "FAE_FERNET_KEY" in caplog.text
    assert "undecryptable" in caplog.text


def test_no_warning_when_the_key_is_configured(caplog):
    import logging

    from app.db.migrate import warn_if_encryption_key_is_ephemeral

    # conftest sets FAE_FERNET_KEY for every test.
    with caplog.at_level(logging.WARNING):
        warn_if_encryption_key_is_ephemeral()

    assert "undecryptable" not in caplog.text
