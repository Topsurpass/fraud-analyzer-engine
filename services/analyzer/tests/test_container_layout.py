"""Structural checks on the container build.

These are deterministic file-layout facts, so they belong in the gate lane and
not in a Docker build: they run in milliseconds and need no daemon. Each one
exists because it broke, or because breaking it produces a failure that only
shows up after a deploy.

The one that started this file: ``fly.toml`` sat at the repo root while the
Dockerfile it named did ``COPY pyproject.toml uv.lock ./``. Fly's build context
is the directory holding ``fly.toml``, the repo root has neither file, so
``fly deploy`` could not build at all. ``test_every_fly_build_context_has_what
_its_dockerfile_copies`` is the general form of that bug.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
DOCKERFILE = SERVICE_DIR / "Dockerfile"
DOCKERIGNORE = SERVICE_DIR / ".dockerignore"


def _dockerfile_lines() -> list[str]:
    """Dockerfile instructions with line continuations and comments folded out."""
    joined: list[str] = []
    buffer = ""
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        joined.append((buffer + line).strip())
        buffer = ""
    if buffer:
        joined.append(buffer.strip())
    return joined


def _ignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _fly_configs() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("fly.toml")
        if ".venv" not in p.parts and ".git" not in p.parts
    )


# --- The build context has to contain what the build copies -----------------


def test_a_fly_config_exists():
    assert _fly_configs(), "no fly.toml anywhere; the deploy config was lost"


@pytest.mark.parametrize("fly_toml", _fly_configs(), ids=lambda p: str(p))
def test_every_fly_build_context_has_what_its_dockerfile_copies(fly_toml: Path):
    """Fly's context is the fly.toml directory. The COPYs must resolve in it."""
    config = tomllib.loads(fly_toml.read_text())
    context = fly_toml.parent
    dockerfile = context / config.get("build", {}).get("dockerfile", "Dockerfile")
    assert dockerfile.is_file(), f"{fly_toml} names a Dockerfile that is not there"

    for line in _dockerfile_lines():
        if not line.upper().startswith("COPY "):
            continue
        parts = line.split()[1:]
        if any(part.startswith("--from=") for part in parts):
            continue  # copied out of another stage, not out of the context
        sources = [p for p in parts[:-1] if not p.startswith("--")]
        for source in sources:
            resolved = context / source
            assert resolved.exists(), (
                f"{dockerfile.name} copies '{source}', which does not exist in the "
                f"build context {context}. Fly builds from the directory holding "
                f"fly.toml, so this deploy cannot build."
            )


def test_service_owns_its_deploy_config():
    """One concern, one directory: fly.toml lives with the code it deploys."""
    assert (SERVICE_DIR / "fly.toml").is_file()


# --- .dockerignore ----------------------------------------------------------


@pytest.mark.parametrize("secret", [".env", ".secrets", "*.db"])
def test_secrets_are_ignored_at_every_depth(secret: str):
    """A bare name only matches the top level; secrets can be nested."""
    assert f"**/{secret}" in _ignore_patterns(), (
        f"'{secret}' must be ignored as '**/{secret}', or a copy of it one "
        f"directory down ships inside the image"
    )


def test_env_example_survives_the_env_ignore():
    patterns = _ignore_patterns()
    assert "!.env.example" in patterns
    assert patterns.index("**/.env.*") < patterns.index("!.env.example")


def test_bytecode_is_ignored_at_every_depth():
    """Host __pycache__ in the image can shadow the source it was built from."""
    assert "**/__pycache__" in _ignore_patterns()


@pytest.mark.parametrize("needed", ["alembic", "alembic.ini", "app", "pyproject.toml"])
def test_runtime_requirements_are_not_ignored(needed: str):
    """FAE_AUTO_MIGRATE=true reads alembic/ at startup, from inside the image."""
    patterns = _ignore_patterns()
    assert needed not in patterns
    assert f"**/{needed}" not in patterns


# --- Dockerfile -------------------------------------------------------------


def test_cmd_is_exec_form():
    """Shell form makes /bin/sh PID 1, so SIGTERM never reaches uvicorn and
    every stop waits out the full kill timeout."""
    cmds = [line for line in _dockerfile_lines() if line.upper().startswith("CMD ")]
    assert cmds, "no CMD"
    for cmd in cmds:
        assert cmd[4:].lstrip().startswith("["), f"CMD is shell form: {cmd}"


def test_dependency_layer_is_separate_from_the_source_copy():
    """A source-only edit must not re-resolve and re-download every dependency."""
    lines = _dockerfile_lines()
    manifest_copy = next(
        i for i, line in enumerate(lines) if line.startswith("COPY pyproject.toml")
    )
    dep_install = next(
        i
        for i, line in enumerate(lines)
        if "uv sync" in line and "--no-install-project" in line
    )
    source_copy = next(i for i, line in enumerate(lines) if line.startswith("COPY . ."))
    assert manifest_copy < dep_install < source_copy


def test_installs_are_frozen():
    """An unfrozen install silently resolves something other than the lockfile."""
    syncs = [line for line in _dockerfile_lines() if "uv sync" in line]
    assert syncs
    assert all("--frozen" in line for line in syncs)


def test_base_image_is_pinned_by_digest():
    text = DOCKERFILE.read_text()
    assert re.search(r"python:3\.13-slim@sha256:[0-9a-f]{64}", text), (
        "pin the base by digest: a floating tag means two builds of the same "
        "commit can ship different interpreters"
    )


def test_runtime_stage_carries_no_build_tooling():
    """uv belongs to the builder stage only."""
    runtime = _dockerfile_lines()
    start = next(i for i, line in enumerate(runtime) if "AS runtime" in line)
    for line in runtime[start:]:
        assert "uv sync" not in line
        assert "astral-sh/uv" not in line


def test_every_path_the_app_writes_to_is_created_writable():
    """The container died at boot with PermissionError: '.secrets' because /app
    is root-owned and the key file is written relative to WORKDIR."""
    from app.security import crypto

    text = DOCKERFILE.read_text()
    key_dir = crypto.KEY_DIR.name
    assert re.search(
        rf"install -d -m 0700 -o analyzer -g analyzer /app/{re.escape(key_dir)}\b",
        text,
    ), (
        f"crypto writes into '{key_dir}/' at startup; the Dockerfile must create "
        f"it owned by the service account or the container cannot start without "
        f"FAE_FERNET_KEY"
    )
