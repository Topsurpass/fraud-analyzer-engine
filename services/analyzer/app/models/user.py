"""Accounts and the sessions they hold open.

The session table exists because a JSON web token cannot be taken away. An
account is deactivated rather than deleted, and a deactivation that takes
effect whenever the holder's token happens to expire is not a deactivation - so
identity is looked up per request against a row this service controls, and
switching off an account ends its sessions at once.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime, new_id
from app.models.enums import UserRole, enum_column


class User(TimestampMixin, Base):
    __tablename__ = "users"

    #: Mirrors the migration's CHECK constraint exactly, so
    #: ``Base.metadata.create_all()`` and ``alembic upgrade head`` emit
    #: identical DDL and ``verify_schema()`` never sees the two disagree.
    #: ``ix_users_email`` below is case-sensitive on both SQLite's and
    #: Postgres's default collations, so without this a mixed-case email and
    #: its lowercase twin both satisfy uniqueness as two different accounts.
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    #: Stored lowercased rather than relying on a case-insensitive collation.
    #: This app runs on both SQLite and Postgres and ``citext`` exists on only
    #: one of them, so case folding happens in Python where both agree.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole), nullable=False, default=UserRole.ANALYST
    )

    #: False blocks login and invalidates live sessions. Accounts are never
    #: deleted, so this is the whole of offboarding.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Set on every admin-issued credential. While true the account may call
    #: nothing but the password change.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    #: When the issued temporary password stops working. Null once the user has
    #: chosen their own, because a password they picked does not expire.
    #:
    #: ``UTCDateTime`` (not a bare ``DateTime(timezone=True)``): this, like
    #: ``locked_until`` below, exists to be compared against an aware
    #: ``utcnow()`` by login logic, and SQLite hands back a naive value on a
    #: real read otherwise - see ``UTCDateTime``'s docstring in
    #: ``app.models.base``.
    temp_password_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Compared against ``utcnow()`` by login logic; see the note on
    #: ``temp_password_expires_at`` above.
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Null for the first admin, who is created by the CLI with nobody logged in.
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


class UserSession(Base):
    """One signed-in browser.

    Named ``UserSession`` because ``sqlalchemy.orm.Session`` is imported in
    nearly every service module here, and two things called ``Session`` in one
    file is a collision that gets resolved wrongly under pressure.
    """

    __tablename__ = "sessions"

    #: A SHA-256 digest of the token, never the token. A database dump
    #: otherwise hands over every live session, which is the same failure as
    #: storing passwords in the clear, one layer up.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: ``UTCDateTime`` rather than a bare ``DateTime(timezone=True)``: every
    #: comparison in ``session_service`` weighs one of these three columns
    #: against an aware ``utcnow()``, on every request, so this table is where
    #: SQLite's naive round-trip (see ``UTCDateTime``'s docstring) would bite
    #: hardest and first.
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: Absolute expiry, fixed at creation and never extended.
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: Moved forward on use, for the idle timeout.
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
