"""Shared fixtures.

Every test gets an isolated app-state DB and a fresh Fernet key, so no test can
observe another's connections, queries, or ciphertext.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    from app.db import app_state
    from app.security.crypto import generate_key, get_fernet

    monkeypatch.setenv("FAE_FERNET_KEY", generate_key())
    monkeypatch.setenv("FAE_APP_DB_URL", f"sqlite:///{tmp_path / 'app_state.db'}")
    get_settings.cache_clear()
    get_fernet.cache_clear()
    app_state.reset_caches()

    app_state.init_db()
    yield

    from app.db import target_registry

    target_registry.dispose_all()
    get_settings.cache_clear()
    get_fernet.cache_clear()
    app_state.reset_caches()


@pytest.fixture
def session():
    from app.db.app_state import get_sessionmaker

    s = get_sessionmaker()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def target_sqlite(tmp_path):
    """A populated SQLite database standing in for a customer's warehouse.

    Deliberately not fraud-shaped in any way the engine knows about: the engine
    must work against whatever schema it is pointed at.
    """
    path = tmp_path / "target.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
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
    )
    conn.commit()
    conn.close()
    return str(path)
