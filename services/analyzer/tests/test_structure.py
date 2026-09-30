"""The folder layout stays navigable: one folder per feature, one obvious place per change.

``STRUCTURE.md`` promises where things live. These tests keep the promise honest
as the code grows, so the map cannot rot into a description of an older layout.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
APP = SERVICE_DIR / "app"
FEATURES = APP / "features"

FEATURE_DIRS = sorted(p for p in FEATURES.iterdir() if p.is_dir() and p.name != "__pycache__")

#: File names a feature may contain. A new name is a decision to write down in
#: STRUCTURE.md, not something to add casually.
KNOWN_ROLES = {
    "__init__", "models", "schemas", "service", "router",
    # feature-specific modules, each described in STRUCTURE.md
    "introspection", "introspection_router", "sessions",
    "execution", "polling", "refresher", "scheduler", "result_cache", "rendered_cache", "sizing",
    "engine", "dismissals", "flagged_rows",
}

#: Package names that the restructure removed. Importing them again means someone
#: is following an old habit and the code has split back into layers.
REMOVED_PACKAGES = ("app.routers", "app.services", "app.schemas", "app.models")


def _py_files():
    for base in (APP, SERVICE_DIR / "tests", SERVICE_DIR / "alembic"):
        yield from base.rglob("*.py")


@pytest.mark.parametrize("feature", FEATURE_DIRS, ids=lambda p: p.name)
def test_feature_files_use_known_role_names(feature):
    unknown = {p.stem for p in feature.glob("*.py")} - KNOWN_ROLES
    assert not unknown, (
        f"{feature.name}/ has {sorted(unknown)}: add the role to KNOWN_ROLES here and to "
        f"STRUCTURE.md, or rename to an existing role"
    )


@pytest.mark.parametrize("feature", FEATURE_DIRS, ids=lambda p: p.name)
def test_every_feature_is_a_package(feature):
    assert (feature / "__init__.py").exists()


def test_removed_layer_packages_are_not_imported_or_recreated():
    for name in ("routers", "services", "schemas", "models"):
        assert not (APP / name).exists(), f"app/{name}/ is back; put it in a feature folder"
    offenders = []
    for path in _py_files():
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            for m in modules:
                if any(m == r or m.startswith(r + ".") for r in REMOVED_PACKAGES):
                    offenders.append(f"{path.relative_to(SERVICE_DIR)}: {m}")
    assert not offenders, offenders


def test_every_models_module_is_registered_for_metadata():
    registry = (APP / "db" / "registry.py").read_text()
    for feature in FEATURE_DIRS:
        if (feature / "models.py").exists():
            assert f"app.features.{feature.name} import models" in registry, (
                f"{feature.name}/models.py is not imported by app/db/registry.py, so its "
                f"tables would be missing from create_all and alembic"
            )


def test_features_do_not_import_each_others_routers():
    offenders = []
    for path in FEATURES.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module.startswith("app.features.") and node.module.endswith((".router", ".introspection_router")):
                offenders.append(f"{path.relative_to(APP)} imports {node.module}")
            if node.module.startswith("app.features.") and node.module.count(".") == 2:
                # `from app.features.x import router as y`
                if any(a.name in ("router", "introspection_router") for a in node.names):
                    offenders.append(f"{path.relative_to(APP)} imports a router from {node.module}")
    assert not offenders, offenders


@pytest.mark.parametrize("feature", [f.name for f in FEATURE_DIRS if (f / "models.py").exists()])
def test_models_module_imports_cleanly_first(feature):
    """Regression for import-order cycles: a fresh interpreter, importing only this module.

    A registry that eagerly imports every model turns "import one model file" into
    an import cycle. The suite as a whole never notices, because something else
    has already imported the registry by the time this one is loaded.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import app.features.{feature}.models"],
        cwd=SERVICE_DIR, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-800:]


def test_structure_map_names_every_feature_and_policy_file():
    text = (SERVICE_DIR / "STRUCTURE.md").read_text()
    for feature in FEATURE_DIRS:
        assert re.search(rf"^\s+{feature.name}/\s", text, re.MULTILINE), (
            f"STRUCTURE.md map does not list {feature.name}/"
        )
    for policy in (APP / "policy").glob("*.py"):
        if policy.stem != "__init__":
            assert re.search(rf"^\s+{policy.name}\s", text, re.MULTILINE), (
                f"STRUCTURE.md map does not list {policy.name}"
            )
