"""Connection profile for a target database.

``password_encrypted`` holds a Fernet token, never a plaintext password, and is
never exposed by any response model.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id
from app.models.enums import ConnectionStatus, DbType, enum_column

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
