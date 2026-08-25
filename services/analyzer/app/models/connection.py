"""Connection profile for a target database.

``password_encrypted`` holds a Fernet token, never a plaintext password, and is
never exposed by any response model.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import ConnectionStatus, DbType, SslMode, enum_column

if TYPE_CHECKING:
    from app.models.saved_query import SavedQuery


class Connection(TimestampMixin, Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    db_type: Mapped[DbType] = mapped_column(enum_column(DbType), nullable=False)

    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    sqlite_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Not optional and not defaulted at the driver's discretion. libpq's own
    # default is "prefer", which offers plaintext first and silently accepts it,
    # so a managed target that requires TLS is unreachable while a target that
    # merely tolerates plaintext is downgraded without anyone being told.
    ssl_mode: Mapped[SslMode] = mapped_column(
        enum_column(SslMode),
        nullable=False,
        default=SslMode.REQUIRE,
        server_default=SslMode.REQUIRE.value,
    )
    # Only read by the two verifying modes. A public CA covers Neon, RDS and
    # friends; an internal CA has nowhere else to go.
    ssl_root_cert: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Paused by a person, not by a failure. Separate from ``status`` on
    #: purpose: status records how the last *test* went, and folding "I turned
    #: this off" into it would destroy the answer to "was it working when I
    #: paused it". While paused the scheduler skips this connection's queries
    #: and its pooled connections are closed, so the engine holds nothing open
    #: against the target.
    paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    status: Mapped[ConnectionStatus] = mapped_column(
        enum_column(ConnectionStatus),
        nullable=False,
        default=ConnectionStatus.UNTESTED,
        server_default=ConnectionStatus.UNTESTED.value,
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    queries: Mapped[list["SavedQuery"]] = relationship(
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
