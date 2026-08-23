"""Schema bootstrap for the app-state database.

A container deploy has no shell step between "image built" and "server
running", so a service that needs a schema has to be able to create its own.
Without this the first request returns ``no such table: connections`` (or
``relation "connections" does not exist`` on Postgres), surfaced as an opaque
500, which says nothing about the actual problem.

``alembic upgrade head`` runs at startup by default. Set ``FAE_AUTO_MIGRATE``
to ``false`` to manage migrations yourself, in which case startup verifies the
schema is present and refuses to serve with a message naming the fix.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.app_state import get_engine
from app.models import Base

logger = logging.getLogger(__name__)

#: Resolved from this file, not the working directory: a container may start
#: the server from anywhere.
ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"


class SchemaNotReadyError(RuntimeError):
    """The app-state database is missing tables the service needs."""


def describe_app_db() -> str:
    """A log-safe description of the app-state database.

    Never returns the password. A startup banner that leaked credentials into
    a hosting provider's log aggregator would be worse than no banner.
    """
    url = make_url(get_settings().resolved_app_db_url)
    return str(url.render_as_string(hide_password=True))


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().resolved_app_db_url)
    return cfg


def missing_tables() -> list[str]:
    """Tables the models declare that the database does not have."""
    inspector = inspect(get_engine())
    present = set(inspector.get_table_names())
    return sorted(set(Base.metadata.tables) - present)


def run_migrations() -> None:
    """Bring the app-state database up to head. Safe to call repeatedly."""
    logger.info("Applying app-state migrations to %s", describe_app_db())
    command.upgrade(alembic_config(), "head")


def verify_schema() -> None:
    """Raise if the schema is not usable, naming the fix."""
    absent = missing_tables()
    if absent:
        raise SchemaNotReadyError(
            f"The app-state database {describe_app_db()} is missing "
            f"{', '.join(absent)}. Run 'alembic upgrade head' against it, or "
            f"set FAE_AUTO_MIGRATE=true to have the service migrate on start."
        )


def warn_if_storage_is_ephemeral() -> None:
    """Say so when saved queries will not survive a restart.

    A container filesystem is normally ephemeral, so a SQLite app-state
    database is silently recreated empty on every deploy. Losing every saved
    connection and query on redeploy should be loud at startup rather than
    discovered later.
    """
    settings = get_settings()
    if not settings.app_db_is_sqlite:
        return
    # The *resolved* file, not the sqlite_app_db_path setting. When
    # FAE_APP_DB_URL overrides the backend, the setting names a file that is
    # not in use, and an operator acting on this warning would go looking in
    # the wrong place.
    location = settings.app_db_sqlite_file or settings.sqlite_app_db_path
    logger.warning(
        "App-state is SQLite at %s. On a container with an ephemeral "
        "filesystem every saved connection and query is lost on restart. "
        "Set FAE_DB_BACKEND=neon and DATABASE_URL for durable storage, or "
        "mount a persistent volume at that path.",
        location,
    )


def warn_if_encryption_key_is_ephemeral() -> None:
    """Say so when stored credentials will not be decryptable after a restart.

    Without ``FAE_FERNET_KEY`` the service generates a key onto local disk. On
    a container that disk is wiped on redeploy, so a fresh key is generated
    while the encrypted passwords sit in a durable database, and every one of
    them becomes permanently unreadable.
    """
    from app.security.crypto import key_is_ephemeral

    if not key_is_ephemeral():
        return
    logger.warning(
        "FAE_FERNET_KEY is not set, so a credential encryption key was "
        "generated on local disk. If this filesystem is ephemeral, every "
        "stored target-database password becomes permanently undecryptable "
        "on the next restart. Set FAE_FERNET_KEY to a fixed value."
    )


def bootstrap_schema() -> None:
    """Startup hook: migrate if allowed, then confirm the schema is usable."""
    settings = get_settings()
    # Report what actually took effect, not just the switch. FAE_APP_DB_URL
    # overrides FAE_DB_BACKEND entirely, and a banner reading "backend=neon"
    # beside a sqlite:// URL is exactly the confusion this line exists to
    # prevent -- the README tells operators to read it to confirm a
    # deployment is configured the way they think it is.
    source = "FAE_APP_DB_URL override" if settings.app_db_url else "FAE_DB_BACKEND"
    logger.info(
        "App-state backend=%s (from %s) url=%s auto_migrate=%s",
        "sqlite" if settings.app_db_is_sqlite else settings.db_backend.value,
        source,
        describe_app_db(),
        settings.auto_migrate,
    )
    warn_if_storage_is_ephemeral()
    warn_if_encryption_key_is_ephemeral()
    if settings.auto_migrate:
        run_migrations()
    verify_schema()
