"""The Alembic migration must produce the same schema the models declare.

Without this, `alembic upgrade head` and `Base.metadata.create_all()` can drift
apart silently, and production ends up with a schema the code does not expect.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "FAE_APP_DB_URL": db_url,
            "FAE_FERNET_KEY": "",
            "PYTHONPATH": str(PROJECT_ROOT),
        },
        capture_output=True,
        text=True,
    )


def test_upgrade_head_matches_model_metadata(tmp_path):
    migrated = tmp_path / "migrated.db"
    result = _run_alembic(f"sqlite:///{migrated}", "upgrade", "head")
    assert result.returncode == 0, result.stderr

    from_models = tmp_path / "from_models.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{from_models}"))

    migrated_inspector = inspect(create_engine(f"sqlite:///{migrated}"))
    models_inspector = inspect(create_engine(f"sqlite:///{from_models}"))

    migrated_tables = set(migrated_inspector.get_table_names()) - {"alembic_version"}
    assert migrated_tables == set(models_inspector.get_table_names())

    for table in sorted(migrated_tables):
        migrated_cols = {
            (c["name"], str(c["type"]), c["nullable"])
            for c in migrated_inspector.get_columns(table)
        }
        model_cols = {
            (c["name"], str(c["type"]), c["nullable"])
            for c in models_inspector.get_columns(table)
        }
        assert migrated_cols == model_cols, f"column drift in {table}"


def test_downgrade_to_base_drops_everything(tmp_path):
    db = tmp_path / "cycle.db"
    url = f"sqlite:///{db}"
    assert _run_alembic(url, "upgrade", "head").returncode == 0
    result = _run_alembic(url, "downgrade", "base")
    assert result.returncode == 0, result.stderr

    remaining = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert remaining == set()
