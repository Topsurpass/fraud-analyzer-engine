"""The sqlite target path allowlist.

A connection profile carries a filesystem path the API process opens directly.
Unconstrained, that is an arbitrary-file-read primitive, and its best target is
the service's own app-state database: registering a connection pointing at it
and selecting from `connections` returns every stored credential ciphertext
through the documented API.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config import get_settings
from app.errors import ErrorCode, InvalidConfigError
from app.security.sqlite_paths import resolve_sqlite_path


@pytest.fixture
def allowed_dir(tmp_path, monkeypatch):
    root = tmp_path / "warehouses"
    root.mkdir()
    monkeypatch.setenv("FAE_SQLITE_ALLOWED_DIRS", str(root))
    get_settings.cache_clear()
    return root


def test_path_inside_an_allowed_directory_is_accepted(allowed_dir):
    target = allowed_dir / "warehouse.db"
    target.touch()
    assert resolve_sqlite_path(str(target)) == target.resolve()


def test_path_in_a_nested_subdirectory_is_accepted(allowed_dir):
    nested = allowed_dir / "team" / "prod"
    nested.mkdir(parents=True)
    target = nested / "w.db"
    target.touch()
    assert resolve_sqlite_path(str(target)) == target.resolve()


def test_path_outside_the_allowlist_is_refused(allowed_dir, tmp_path):
    outside = tmp_path / "elsewhere.db"
    outside.touch()
    with pytest.raises(InvalidConfigError) as ei:
        resolve_sqlite_path(str(outside))
    assert ei.value.error_code == ErrorCode.INVALID_CONNECTION_CONFIG


def test_traversal_out_of_an_allowed_directory_is_refused(allowed_dir):
    """Resolution happens before comparison, so `..` cannot escape."""
    escape = str(allowed_dir / ".." / ".." / "etc" / "passwd")
    with pytest.raises(InvalidConfigError):
        resolve_sqlite_path(escape)


def test_symlink_out_of_an_allowed_directory_is_refused(allowed_dir, tmp_path):
    """A prefix check on the raw string would admit this; resolve() does not."""
    secret = tmp_path / "secret.db"
    secret.touch()
    link = allowed_dir / "innocent.db"
    link.symlink_to(secret)
    with pytest.raises(InvalidConfigError):
        resolve_sqlite_path(str(link))


def test_sibling_directory_sharing_a_name_prefix_is_refused(tmp_path, monkeypatch):
    """"/data-private" must not pass an allowlist of "/data"."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data-private").mkdir()
    monkeypatch.setenv("FAE_SQLITE_ALLOWED_DIRS", str(tmp_path / "data"))
    get_settings.cache_clear()

    sneaky = tmp_path / "data-private" / "x.db"
    sneaky.touch()
    with pytest.raises(InvalidConfigError):
        resolve_sqlite_path(str(sneaky))


def test_empty_allowlist_refuses_every_sqlite_target(tmp_path, monkeypatch):
    """The right setting for a container, where sqlite targets cannot work."""
    monkeypatch.setenv("FAE_SQLITE_ALLOWED_DIRS", "")
    get_settings.cache_clear()
    target = tmp_path / "w.db"
    target.touch()
    with pytest.raises(InvalidConfigError) as ei:
        resolve_sqlite_path(str(target))
    assert "disabled" in ei.value.message


def test_app_state_database_is_refused_even_when_inside_the_allowlist(
    tmp_path, monkeypatch
):
    """The core escalation, refused regardless of what the allowlist permits.

    Reading this file through a target connection returns every Fernet
    ciphertext in `connections` and every saved query.
    """
    app_db = tmp_path / "app_state.db"
    app_db.touch()
    monkeypatch.setenv("FAE_APP_DB_URL", f"sqlite:///{app_db}")
    monkeypatch.setenv("FAE_SQLITE_ALLOWED_DIRS", str(tmp_path))
    get_settings.cache_clear()

    with pytest.raises(InvalidConfigError) as ei:
        resolve_sqlite_path(str(app_db))
    assert "app-state database" in ei.value.message


def test_empty_path_is_refused(allowed_dir):
    with pytest.raises(InvalidConfigError):
        resolve_sqlite_path("")
