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
