"""Operator commands that need no running server and no signed-in user.

The first administrator is created here and nowhere else. An HTTP endpoint that
mints an administrator is reachable by anything that can reach the service; a
command is reachable by somebody who can already read the database, which is
the bar this is meant to sit at. Deliberately no ``/auth/bootstrap`` route
exists for this reason - do not add one.
"""

from __future__ import annotations

from datetime import timedelta

import typer
from sqlalchemy import func, inspect, select

from app.db.app_state import get_engine, get_sessionmaker
from app.errors import AppError
from app.models.base import utcnow
from app.models.enums import UserRole
from app.models.user import User
from app.security import passwords

app = typer.Typer(help="Switchboard operator commands.", no_args_is_help=True)

#: How long an issued temporary password stays usable. An unclaimed credential
#: valid forever in somebody's chat history is a standing liability.
TEMP_PASSWORD_HOURS = 72


def _target_description() -> str:
    """The database about to be written to, with any password removed.

    ``render_as_string(hide_password=True)`` rather than the raw URL string:
    an app-state DSN carries a live credential, and a command whose job is to
    reassure the operator which database it is touching must not do that by
    echoing a secret to the same terminal.
    """
    url = get_engine().url
    return url.render_as_string(hide_password=True)


def _require_schema() -> None:
    """Refuse to run before migrations have.

    Without this, running from the wrong directory creates an administrator in
    the SQLite fallback while the real database stays empty - and the only
    symptom is "invalid credentials" at a login page, with nothing anywhere
    explaining why. Every command below calls this first.
    """
    if "users" not in inspect(get_engine()).get_table_names():
        typer.secho(
            f"No users table in {_target_description()}.\n"
            "Run the migrations first, and check you are in services/analyzer.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)


@app.command("create-admin")
def create_admin(
    email: str = typer.Option(..., prompt=True),
    name: str = typer.Option(..., prompt="Full name"),
) -> None:
    """Create the first administrator.

    The only way an admin account is ever minted. Everything after this is
    the admin's own doing, from inside the application, with a session.
    """
    _require_schema()
    typer.echo(f"Target database: {_target_description()}")

    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    try:
        passwords.validate_password_strength(password)
    except AppError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    # Mirrors the ck_users_email_lowercase CHECK constraint: an unnormalised
    # insert is rejected by the database, but failing here gives a clearer
    # message than a raw IntegrityError would.
    normalised = email.strip().lower()
    db = get_sessionmaker()()
    try:
        existing = db.scalar(select(User).where(func.lower(User.email) == normalised))
        if existing is not None:
            typer.secho(f"{normalised} already has an account.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        db.add(
            User(
                email=normalised,
                full_name=name.strip(),
                password_hash=passwords.hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
                # They chose it at the prompt, so there is nothing to replace.
                # Forcing a change here would trap the first admin behind a
                # screen with nobody able to reset them - the opposite of
                # reset-password below, which issues a credential nobody
                # chose and so must be replaced.
                must_change_password=False,
            )
        )
        db.commit()
    finally:
        db.close()

    typer.secho(f"Administrator created: {normalised}", fg=typer.colors.GREEN)


@app.command("reset-password")
def reset_password(email: str = typer.Option(..., prompt=True)) -> None:
    """Issue a temporary password for an account that is locked out.

    The opposite bootstrap problem to create-admin: this account already
    exists and its owner cannot reach it, so the operator hands them a
    credential nobody chose, which is exactly why it must expire and be
    replaced at next sign-in rather than kept.
    """
    _require_schema()
    normalised = email.strip().lower()

    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(func.lower(User.email) == normalised))
        if user is None:
            typer.secho(f"No account for {normalised}.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        temporary = passwords.generate_temporary_password()
        user.password_hash = passwords.hash_password(temporary)
        user.must_change_password = True
        user.temp_password_expires_at = utcnow() + timedelta(hours=TEMP_PASSWORD_HOURS)
        user.failed_login_count = 0
        user.locked_until = None
        db.commit()

        # A locked-out account may still hold live sessions from before it was
        # locked (a stolen credential rather than a forgotten one), and a
        # reset that leaves those sessions standing hands back the door it
        # just changed the lock on.
        from app.services import session_service

        session_service.revoke_all_for_user(db, user.id)
    finally:
        db.close()

    typer.echo("")
    typer.secho(f"Temporary password: {temporary}", fg=typer.colors.YELLOW, bold=True)
    typer.echo(
        f"Shown once, and only here. Valid for {TEMP_PASSWORD_HOURS} hours; "
        "they must choose a new password at first sign-in."
    )


@app.command("list-users")
def list_users() -> None:
    """Every account, with its role and whether it is switched on."""
    _require_schema()
    db = get_sessionmaker()()
    try:
        users = db.scalars(select(User).order_by(User.email)).all()
        if not users:
            typer.echo("No accounts yet. Run: fae create-admin")
            return
        for user in users:
            state = "active" if user.is_active else "inactive"
            typer.echo(f"{user.email:40} {user.role.value:8} {state}")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover - console script is the entry point
    app()
