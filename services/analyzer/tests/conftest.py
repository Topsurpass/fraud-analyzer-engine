"""Shared fixtures.

Every test gets an isolated app-state DB and a fresh Fernet key, so no test can
observe another's connections, queries, or ciphertext.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from app.config import get_settings

#: Modules whose tests stand up a database, an HTTP client, or a migration.
#: Everything else is the fast gate lane, meant to run on every commit.
_INTEGRATION_MODULES = {
    "test_connections_api",
    "test_dashboards_api",
    "test_introspection_api",
    "test_queries_api",
    "test_run_poll_api",
    "test_error_mapping",
    "test_models",
    "test_migrations",
    "test_target_registry",
    "test_sqlite_path_api",
    "test_batch_api",
    "test_health_api",
    "test_ratelimit_api",
    "test_openapi_contract",
    "test_log_retention",
    "test_query_efficiency",
    "test_result_limits",
    "test_observability",
    "test_flag_rules_api",
}


def pytest_collection_modifyitems(items):
    """Mark by module, so a new test file joins the right lane by name alone."""
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1]
        if module == "test_live_targets":
            item.add_marker(pytest.mark.live)
            item.add_marker(pytest.mark.integration)
        elif module in _INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Point every test at its own app-state DB and Fernet key.

    Deliberately cheap: it only sets environment and clears caches. Creating
    the schema is left to the `app_db` fixture, so the large unit-test files
    (SQL guard, errors, config) never pay for a database they do not touch.
    """
    from app import ratelimit
    from app.db import app_state
    from app.security import sql_guard
    from app.security.crypto import generate_key, get_fernet
    from app.services import result_cache

    monkeypatch.setenv("FAE_FERNET_KEY", generate_key())
    monkeypatch.setenv("FAE_APP_DB_URL", f"sqlite:///{tmp_path / 'app_state.db'}")
    # Keep an unreachable host from stalling a test for the production default.
    monkeypatch.setenv("FAE_CONNECT_TIMEOUT_S", "1")
    # Target sqlite files live under the per-test tmp_path, which is outside
    # the default allowlist. Opting the tmp directory in keeps the guard
    # switched on everywhere else, so a path-escape test still has something
    # real to fail against. tests/test_sqlite_paths.py sets its own values.
    monkeypatch.setenv("FAE_SQLITE_ALLOWED_DIRS", str(tmp_path))
    # The app_db fixture already builds the schema with create_all, so running
    # Alembic again in every TestClient startup would be pure cost. The tests
    # that exercise the startup migration path set this back to true.
    monkeypatch.setenv("FAE_AUTO_MIGRATE", "false")
    # Access logs are proven by tests/test_observability.py; everywhere else
    # they bury the actual assertion failure in pytest's captured output.
    monkeypatch.setenv("FAE_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    get_fernet.cache_clear()
    app_state.reset_caches()
    sql_guard.clear_validation_cache()
    result_cache.clear()
    ratelimit.reset()

    yield

    from app.db import target_registry

    target_registry.dispose_all()
    get_settings.cache_clear()
    get_fernet.cache_clear()
    app_state.reset_caches()
    sql_guard.clear_validation_cache()
    result_cache.clear()
    ratelimit.reset()


@pytest.fixture
def app_db():
    """Create the app-state schema. Requested only by tests that need it."""
    from app.db import app_state

    app_state.init_db()
    return app_state


@pytest.fixture
def session(app_db):
    from app.db.app_state import get_sessionmaker

    s = get_sessionmaker()()
    try:
        yield s
    finally:
        s.close()


TARGET_SCHEMA = """
CREATE TABLE txns (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0,
    comment TEXT
);
INSERT INTO txns (day, user_id, amount, flagged, comment) VALUES
    ('2026-08-18', 1, 10.5, 0, 'ok'),
    ('2026-08-19', 1, 900.0, 1, 'velocity'),
    ('2026-08-19', 2, 12.0, 0, NULL),
    ('2026-08-20', 3, 750.25, 1, 'geo mismatch'),
    ('2026-08-21', 3, 20.0, 0, 'ok');
CREATE VIEW flagged_txns AS SELECT * FROM txns WHERE flagged = 1;
"""


@pytest.fixture(scope="session")
def _target_template(tmp_path_factory):
    """Build the seeded target database once per session.

    Running the DDL per test cost about 80ms each, which dominated the fast
    lane. Copying a prepared file is roughly a millisecond and gives every test
    the same pristine, independently writable database.
    """
    path = tmp_path_factory.mktemp("template") / "target.db"
    conn = sqlite3.connect(path)
    conn.executescript(TARGET_SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def target_sqlite(tmp_path, _target_template):
    """A populated SQLite database standing in for a customer's warehouse.

    Deliberately not fraud-shaped in any way the engine knows about: the engine
    must work against whatever schema it is pointed at. Each test gets its own
    copy, so a test that writes cannot affect another.
    """
    path = tmp_path / "target.db"
    shutil.copyfile(_target_template, path)
    return str(path)


@pytest.fixture
def client(app_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def sqlite_connection(client, target_sqlite):
    """A created, tested-OK connection pointed at the temp SQLite target."""
    response = client.post(
        "/connections",
        json={"name": "target", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["test_ok"] is True, body
    return body["connection"]
