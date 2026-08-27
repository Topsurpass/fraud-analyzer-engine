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
    "test_audit_log",
    "test_auth_api",
    "test_role_enforcement",
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
    "test_cli",
    "test_ownership",
    "test_password_change_gate",
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
    # The scheduler queries target databases on a timer. A test suite must not
    # start one: it would run every fixture's queries in the background, at
    # unpredictable moments, against tables another test is asserting on.
    # tests/test_scheduler.py drives it directly instead.
    monkeypatch.setenv("FAE_SCHEDULER_ENABLED", "false")
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
def admin_client(client, app_db):
    """A TestClient carrying an admin session.

    Existing tests predate authentication and assert behaviour rather than
    permissions, so they run as an admin. The permission rules themselves are
    covered by tests/test_role_enforcement.py, which uses both roles
    deliberately.

    Mutates and returns the same ``client`` object rather than building a
    second one: every other fixture in this file (``sqlite_connection``,
    anything built on ``client``) already depends on ``client`` specifically,
    and ``client`` is function-scoped, so mutating its headers here cannot
    leak a session into another test.
    """
    from tests.test_auth_api import login, make_user
    from app.models.enums import UserRole

    # ``.test`` is one of the four RFC 2606 reserved TLDs (alongside
    # ``.example``, ``.invalid``, ``.localhost``) and pydantic's ``EmailStr``
    # hard-rejects it via email-validator's special-use-domain check, even
    # with deliverability checking off - so this has to use a domain that
    # actually resolves the request-body validation on POST /auth/login,
    # not just the ORM's own lowercase CHECK constraint. ``example.com`` is
    # the same domain the rest of the suite's fixtures already use.
    make_user(email="suite-admin@example.com", role=UserRole.ADMIN)
    token = login(client, email="suite-admin@example.com").json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def sqlite_connection(admin_client, target_sqlite):
    """A created, tested-OK connection pointed at the temp SQLite target.

    Creating a connection is an admin-only write (see
    app/routers/connections.py), so this rides on admin_client rather than the
    bare client - every test that pulls in sqlite_connection gets an
    authenticated admin session as a side effect, which is what it needs to
    make the POST below succeed in the first place.
    """
    response = admin_client.post(
        "/connections",
        json={"name": "target", "db_type": "sqlite", "sqlite_path": target_sqlite},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["test_ok"] is True, body
    return body["connection"]


@pytest.fixture(autouse=True)
def _no_dns_in_tests(request, monkeypatch):
    """Keep address pinning from doing real DNS during the suite.

    ``postgres_connect_args`` resolves the target hostname so it can hand libpq
    only the addresses this machine can reach. Test connections point at names
    like ``db.example.test`` that do not exist, so without this every test that
    builds connect args pays a real lookup and a real NXDOMAIN timeout - the
    gate lane went from two seconds to minutes.

    Returning "no opinion" is also the pre-existing behaviour, so tests that
    are not about addressing see exactly what they saw before. The addressing
    tests opt back in by patching the seams themselves.
    """
    # The addressing tests are about this function, so stubbing it there would
    # have them assert the stub.
    if request.node.get_closest_marker("real_addressing"):
        return

    from app.db import addressing

    addressing.routable_addresses.cache_clear()
    monkeypatch.setattr(addressing, "routable_addresses", lambda host, port: ())
    monkeypatch.setattr(
        "app.db.target_registry.routable_addresses", lambda host, port: ()
    )
