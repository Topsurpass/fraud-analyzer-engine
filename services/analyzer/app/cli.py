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
from sqlalchemy import delete, func, inspect, select, update

from app.db.app_state import get_engine, get_sessionmaker
from app.errors import AppError
from app.models.base import utcnow
from app.models.dashboard import Dashboard
from app.models.enums import UserRole
from app.models.saved_query import SavedQuery
from app.models.user import User, UserSession
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


def _unowned_counts(db) -> tuple[int, int]:
    """How many saved queries and dashboards have no owner."""
    queries = db.scalar(
        select(func.count()).select_from(SavedQuery).where(SavedQuery.owner_id.is_(None))
    )
    dashboards = db.scalar(
        select(func.count()).select_from(Dashboard).where(Dashboard.owner_id.is_(None))
    )
    return int(queries or 0), int(dashboards or 0)


def _claim_unowned(db, admin_id: str) -> None:
    """Give every unowned saved query and dashboard to the new administrator.

    One transaction for both tables: claiming half of somebody's work is worse
    than claiming none of it, because the half that moved is no longer
    identifiable as needing the other half.
    """
    db.execute(
        update(SavedQuery).where(SavedQuery.owner_id.is_(None)).values(owner_id=admin_id)
    )
    db.execute(
        update(Dashboard).where(Dashboard.owner_id.is_(None)).values(owner_id=admin_id)
    )
    db.commit()


def _offer_to_claim(db, admin_id: str, claim: bool | None) -> None:
    """Report what nobody owns, and offer to hand it to the new administrator.

    Ownership columns are nullable because rows predating accounts have no
    owner and there was no administrator at migration time to attribute them
    to. Nothing else in the application can ever set an owner on those rows,
    so without this they stay ``owner_id IS NULL`` permanently: administrators
    can still see them, no analyst can, and the person who wrote them can
    never edit them again. This command is the only place that offer exists,
    which is why the design names it twice.

    ``claim`` is the ``--claim/--no-claim`` flag: None means ask. The prompt
    defaults to claiming, because the operator running ``create-admin`` is the
    first administrator and leaving the rows unowned is the outcome that
    nothing later can undo.
    """
    queries, dashboards = _unowned_counts(db)
    if not queries and not dashboards:
        return

    typer.echo("")
    typer.echo(
        f"Unowned work found: {queries} saved "
        f"{'query' if queries == 1 else 'queries'} and {dashboards} "
        f"{'dashboard' if dashboards == 1 else 'dashboards'}. "
        "Nothing else can give these an owner later."
    )
    if claim is None:
        claim = typer.confirm("Give them to this administrator?", default=True)
    if not claim:
        typer.echo("Left unowned. Only administrators will see them.")
        return

    _claim_unowned(db, admin_id)
    typer.secho(
        f"Claimed {queries + dashboards} rows for this administrator.",
        fg=typer.colors.GREEN,
    )


@app.command("create-admin")
def create_admin(
    email: str = typer.Option(..., prompt=True),
    name: str = typer.Option(..., prompt="Full name"),
    claim: bool | None = typer.Option(
        None,
        "--claim/--no-claim",
        help=(
            "Claim saved queries and dashboards that have no owner, or leave "
            "them unowned. Omit to be asked."
        ),
    ),
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

        admin = User(
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
        db.add(admin)
        db.commit()

        typer.secho(f"Administrator created: {normalised}", fg=typer.colors.GREEN)
        # After the commit, deliberately. The account exists whatever happens
        # next; a declined offer, or an interrupted prompt, must not undo the
        # one thing this command is for.
        _offer_to_claim(db, admin.id, claim)
    finally:
        db.close()


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
        # Inlined rather than calling session_service.revoke_all_for_user:
        # that helper commits on its own, which would split this into two
        # transactions - the password hash landing while the old sessions
        # survive if the second commit failed. A reset is issued exactly
        # when an account is suspected compromised (locked out, credential
        # handed to the wrong person), which is precisely the moment a
        # stolen-but-still-live session must die together with the password
        # that let it in, not in a second write that can fail independently
        # and leave the door standing open behind the new lock.
        db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        db.commit()
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
