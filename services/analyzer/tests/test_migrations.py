"""The Alembic migration must produce the schema the models declare.

Without this, ``alembic upgrade head`` and ``Base.metadata.create_all()`` can
drift apart silently and production ends up with a schema the code does not
expect. Alembic runs in-process rather than as a subprocess: a subprocess pays
a full interpreter start per invocation and buys nothing here, since env.py
reads its URL from the same setting the application uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alembic_for(monkeypatch):
    def _configure(db_url: str) -> Config:
        monkeypatch.setenv("FAE_APP_DB_URL", db_url)
        get_settings.cache_clear()
        cfg = Config()  # no ini file, so env.py skips fileConfig
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", db_url)
        return cfg

    return _configure


def _schema_of(url: str) -> dict[str, set[tuple[str, str, bool]]]:
    inspector = inspect(create_engine(url))
    return {
        table: {
            (c["name"], str(c["type"]), bool(c["nullable"]))
            for c in inspector.get_columns(table)
        }
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def test_upgrade_head_matches_model_metadata(tmp_path, alembic_for):
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(alembic_for(migrated_url), "head")

    from_models_url = f"sqlite:///{tmp_path / 'from_models.db'}"
    Base.metadata.create_all(create_engine(from_models_url))

    migrated = _schema_of(migrated_url)
    from_models = _schema_of(from_models_url)

    assert set(migrated) == set(from_models)
    for table in sorted(migrated):
        assert migrated[table] == from_models[table], f"column drift in {table}"


def test_migration_creates_the_expected_tables(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'tables.db'}"
    command.upgrade(alembic_for(url), "head")
    assert set(_schema_of(url)) == {
        "connections",
        "saved_queries",
        "query_execution_logs",
        "dashboards",
        "dashboard_items",
        "flag_rules",
        "flag_conditions",
        "flag_dismissals",
        "flagged_rows",
        "query_charts",
        "users",
        "sessions",
    }


def test_migration_sets_on_delete_cascade(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    command.upgrade(alembic_for(url), "head")
    inspector = inspect(create_engine(url))
    for table in (
        "saved_queries",
        "query_execution_logs",
        "dashboard_items",
        # A rule must not outlive its query, nor a condition its rule.
        "flag_rules",
        "flag_conditions",
    ):
        fks = inspector.get_foreign_keys(table)
        assert fks, f"{table} has no foreign key"
        # dashboard_items has two, and both must cascade: a board must not
        # outlive its dashboard, nor keep a card for a deleted query.
        for fk in fks:
            assert fk["options"].get("ondelete") == "CASCADE", (
                f"{table}.{fk['constrained_columns']}"
            )


def test_downgrade_to_base_drops_everything(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'cycle.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert _schema_of(url) == {}


def test_ssl_mode_backfills_existing_connections(tmp_path, alembic_for):
    """A connection saved before 0006 must come out of it with TLS on.

    Backfilling to libpq's effective old default ("prefer") would preserve the
    bug for every row that already existed - a managed target stays unreachable
    and a permissive one stays silently downgradable - which is the opposite of
    what the migration is for.
    """
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "0005_flag_rules")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connections (id, name, db_type, host, database, "
                "username, status, created_at, updated_at) VALUES "
                "('c1', 'legacy', 'postgres', 'db.example.test', 'd', 'u', "
                "'untested', '2026-08-01', '2026-08-01')"
            )
        )

    command.upgrade(cfg, "head")

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT ssl_mode, ssl_root_cert FROM connections WHERE id = 'c1'")
        ).one()
    assert row[0] == "require"
    assert row[1] is None


def test_ssl_columns_survive_a_downgrade_and_reapply(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0005_flag_rules")
    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("connections")}
    assert "ssl_mode" not in columns
    assert "ssl_root_cert" not in columns

    command.upgrade(cfg, "head")
    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("connections")}
    assert {"ssl_mode", "ssl_root_cert"} <= columns


def test_flag_dismissals_cascade_from_their_query(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    command.upgrade(alembic_for(url), "head")

    inspector = inspect(create_engine(url))
    keys = inspector.get_foreign_keys("flag_dismissals")
    assert any(
        key["referred_table"] == "saved_queries" and key["options"].get("ondelete") == "CASCADE"
        for key in keys
    )
    # One row cannot be dismissed twice on the same query.
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("flag_dismissals")}
    assert ("query_id", "row_fingerprint") in uniques


def test_surge_threshold_leaves_existing_charts_unset(tmp_path, alembic_for):
    """A chart from before 0010 comes out of it following the app default.

    Backfilling the default value into the column would look identical today
    and behave differently forever after: "unset" would become "pinned to
    whatever the default happened to be on migration day", and moving the
    default later would leave every pre-existing chart behind. NULL is the only
    value that keeps the distinction.
    """
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "0009_query_charts")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO saved_queries (id, connection_id, name, sql_text, "
                "row_limit, poll_interval_ms, created_at, updated_at) VALUES "
                "('q1', 'c1', 'Q', 'SELECT 1', 100, 5000, '2026-08-01', '2026-08-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO query_charts (id, query_id, name, position, "
                "chart_type, created_at, updated_at) VALUES "
                "('ch1', 'q1', 'Chart', 0, 'line', '2026-08-01', '2026-08-01')"
            )
        )

    command.upgrade(cfg, "head")

    with engine.begin() as conn:
        value = conn.execute(
            text("SELECT surge_threshold_pct FROM query_charts WHERE id = 'ch1'")
        ).scalar_one()
    assert value is None


def test_surge_threshold_survives_a_downgrade_and_reapply(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0009_query_charts")
    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("query_charts")}
    assert "surge_threshold_pct" not in columns

    command.upgrade(cfg, "head")
    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("query_charts")}
    assert "surge_threshold_pct" in columns


def test_users_and_sessions_are_created(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "sessions"} <= tables


def test_a_session_is_removed_with_its_user(tmp_path, alembic_for):
    """Sessions cascade even though users are never deleted.

    The spec forbids deleting accounts, so this should never fire in practice.
    It is here because "never happens" and "cannot happen" are different, and a
    session row pointing at a missing user would authenticate nobody while
    looking like it authenticates someone.
    """
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash, role, "
                "is_active, must_change_password, failed_login_count, "
                "created_at, updated_at) VALUES ('u1', 'a@b.test', 'A', 'x', "
                "'admin', 1, 0, 0, '2026-08-26', '2026-08-26')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, "
                "last_seen_at) VALUES ('s1', 'u1', '2026-08-26', '2026-08-27', "
                "'2026-08-26')"
            )
        )
        conn.execute(text("DELETE FROM users WHERE id = 'u1'"))
        remaining = conn.execute(text("SELECT count(*) FROM sessions")).scalar_one()
    assert remaining == 0


def test_users_and_sessions_survive_a_downgrade_and_reapply(tmp_path, alembic_for):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0010_surge_threshold")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert "users" not in tables
    assert "sessions" not in tables

    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "sessions"} <= tables


def test_a_mixed_case_email_is_rejected_by_the_database(tmp_path, alembic_for):
    """Guards the case a future write path forgets to normalize.

    ``ix_users_email`` is a case-sensitive unique index on both SQLite and
    Postgres default collations, so without a CHECK constraint
    "Kemi@x.test" and "kemi@x.test" both satisfy uniqueness as two separate
    accounts. Normalizing in Python (``.lower()`` before insert) only holds
    for as long as every write site remembers to do it -- the next write
    path (an admin-created user, a bulk import) is exactly the site that
    forgets. The CHECK constraint makes the bad row impossible to store at
    all, regardless of which code path attempted it.
    """
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = alembic_for(url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with pytest.raises(IntegrityError, match="ck_users_email_lowercase"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash, "
                    "role, is_active, must_change_password, "
                    "failed_login_count, created_at, updated_at) VALUES "
                    "('u1', 'Kemi@x.test', 'A', 'x', 'admin', 1, 0, 0, "
                    "'2026-08-26', '2026-08-26')"
                )
            )
