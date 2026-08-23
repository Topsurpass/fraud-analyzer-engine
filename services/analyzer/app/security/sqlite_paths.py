"""Where a sqlite *target* connection is allowed to point.

A connection profile carries a filesystem path that the API process opens
directly. Unconstrained, that makes the profile an arbitrary-file-read
primitive rather than a database connection: whatever the service user can
read, the ordinary query endpoints will return.

The worst case is not a stray ``/etc/passwd``. It is the service's own
app-state database. Registering a sqlite connection pointing at it and running
``SELECT name, password_encrypted FROM connections`` returns every stored
credential ciphertext through the documented, unauthenticated API. Every saved
query and execution log comes out the same way.

Two rules, both enforced here so there is one place to read and one place to
change:

1. The resolved path must sit under one of ``FAE_SQLITE_ALLOWED_DIRS``.
2. The app-state database is refused outright, whatever the allowlist says.

Paths are compared after ``resolve()``, which follows symlinks and collapses
``..``. Comparing the raw string would let ``./data/../../etc/passwd`` pass a
prefix check while opening something else entirely.

This is a *containment* control, not an authentication one. It does not decide
who may register a connection; it bounds the damage of the ones they can.
"""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.errors import InvalidConfigError


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or sits underneath it.

    ``Path.is_relative_to`` compares resolved components, so a sibling
    directory sharing a name prefix ("/data-private" against an allowed
    "/data") is correctly excluded where a raw ``startswith`` would admit it.
    """
    return path == root or path.is_relative_to(root)


def resolve_sqlite_path(raw_path: str) -> Path:
    """Validate a target sqlite path and return it resolved.

    Every caller that is about to open a sqlite target goes through this, so a
    connection row written before the allowlist existed is checked on use
    rather than being trusted because it is already in the database.

    Raises:
        InvalidConfigError: the path is empty, outside the allowlist, or is
            the service's own app-state database.
    """
    if not raw_path:
        raise InvalidConfigError("A sqlite connection requires 'sqlite_path'.")

    settings = get_settings()
    resolved = Path(raw_path).expanduser().resolve()

    app_db = settings.app_db_sqlite_file
    if app_db is not None and resolved == app_db:
        raise InvalidConfigError(
            "That path is this service's own app-state database. Reading it "
            "through a target connection would expose every stored "
            "credential, so it is refused.",
            {"sqlite_path": raw_path},
        )

    allowed = settings.sqlite_allowed_dir_list
    if not allowed:
        raise InvalidConfigError(
            "sqlite target connections are disabled: FAE_SQLITE_ALLOWED_DIRS "
            "is empty. Set it to the directory holding the database file, or "
            "use a postgres/mysql connection.",
            {"sqlite_path": raw_path},
        )

    if not any(_is_within(resolved, root) for root in allowed):
        raise InvalidConfigError(
            f"sqlite_path is outside the allowed directories. Allowed: "
            f"{', '.join(str(root) for root in allowed)}.",
            {"sqlite_path": raw_path, "resolved": str(resolved)},
        )

    return resolved
