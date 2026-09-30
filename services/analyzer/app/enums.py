"""Enums shared across features: the small closed vocabularies every layer agrees on.

Options that operators edit (chart types, flag operators, allowed SQL) live in
``app/policy/`` instead.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum


class DbType(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"


class ConnectionStatus(StrEnum):
    UNTESTED = "untested"
    OK = "ok"
    FAILED = "failed"


class SslMode(StrEnum):
    """How much TLS a target connection insists on.

    libpq's own vocabulary, so the Postgres mapping is the identity function and
    nobody has to hold a translation table in their head while reading a
    connection row. MySQL has no equivalent parameter and is mapped in
    ``app.db.target_registry.mysql_connect_args``.

    Ordered weakest to strongest. The two ``verify`` modes are the only ones
    that authenticate the server rather than merely encrypting the wire, so they
    are the only ones a root certificate applies to.
    """

    DISABLE = "disable"
    ALLOW = "allow"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


#: Modes that check the server's certificate, and so can read a root cert.
VERIFYING_SSL_MODES = frozenset({SslMode.VERIFY_CA, SslMode.VERIFY_FULL})


class UserRole(StrEnum):
    """What a signed-in person may do.

    Two roles on purpose. A third ("viewer", "supervisor") is a permission
    system in disguise, and the moment roles need combining they should become
    a permission table rather than a longer enum.
    """

    ADMIN = "admin"
    ANALYST = "analyst"


class AuditAction(StrEnum):
    """Administrative acts worth reconstructing months later.

    Deliberately a closed set rather than free text. An audit trail whose
    action names are typed by whoever wrote the call site cannot be filtered
    or counted, and the first person to need it is looking for one specific
    thing under time pressure.
    """

    USER_CREATED = "user_created"
    USER_DEACTIVATED = "user_deactivated"
    USER_REACTIVATED = "user_reactivated"
    USER_ROLE_CHANGED = "user_role_changed"
    USER_PASSWORD_RESET = "user_password_reset"


def enum_column(enum_cls: type[StrEnum], length: int = 20) -> Enum:
    """A VARCHAR column that stores an enum's *value*, not its name.

    SQLAlchemy persists ``Enum(SomeEnum)`` by member name by default, so
    ``DbType.SQLITE`` lands in the database as ``"SQLITE"`` while the JSON API
    speaks ``"sqlite"``. Two things break as a result:

    * ``server_default`` is written as a value, so any row created by a
      migration, a seed script, or plain SQL is unreadable by the ORM. It
      raises ``LookupError: 'sqlite' is not among the defined enum values``.
    * Anything reading the database directly, which for this service means a
      frontend pointed at the same Postgres, sees different strings than the
      API returns for the same field.

    ``values_callable`` makes storage and the API agree on one spelling.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )
