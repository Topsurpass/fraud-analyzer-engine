"""Logging in, logging out, and reporting who is signed in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.errors import AppError, ErrorCode
from app.models.user import User, UserSession
from app.schemas.auth import ChangePasswordRequest, LoginRequest, LoginResponse, UserRead
from app.services import auth_service, session_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def current_user(
    request: Request, db: Session = Depends(get_session)
) -> User:
    """The signed-in user, or 401.

    Lives here rather than in the dependency module because ``/auth/me`` and
    ``/auth/logout`` need it before the general-purpose dependencies exist, and
    Task 5 re-exports it rather than writing a second copy.
    """
    user = session_service.resolve(db, _bearer(request))
    if user is None:
        raise AppError(ErrorCode.NOT_AUTHENTICATED, "Sign in to continue.")
    return user


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest, request: Request, db: Session = Depends(get_session)
) -> LoginResponse:
    user = auth_service.authenticate(db, body.email, body.password)
    token = session_service.issue(
        db,
        user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return LoginResponse(token=token, user=UserRead.model_validate(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: Session = Depends(get_session)) -> Response:
    # Deliberately not behind ``current_user``: logging out with an already-dead
    # session must succeed, or a user whose session expired cannot clear it.
    session_service.revoke(db, _bearer(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(current_user)) -> UserRead:
    return UserRead.model_validate(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    # Read this session's own row before anything below touches the table:
    # revoke_all_for_user deletes it along with every other session for this
    # user, and its created_at/expires_at are what issue_with_id restores
    # the row with afterwards. Looked up rather than trusted from a request
    # header because ``mine`` is a digest, and the row is the only place the
    # original absolute expiry lives.
    mine = session_service.digest(_bearer(request))
    mine_row = db.get(UserSession, mine)

    auth_service.change_password(db, user, body.current_password, body.new_password)

    # Every session but this one. Being signed out of the browser you just used
    # to change your password is a bug; leaving an intercepted session alive is
    # a hole. The one that survives keeps its original absolute expiry rather
    # than a fresh session_absolute_hours window: a password change proves the
    # current password, it is not a new login, and resetting the clock here
    # would let anyone dodge the absolute cap indefinitely by changing their
    # password on a schedule. See issue_with_id's docstring.
    session_service.revoke_all_for_user(db, user.id)
    if mine_row is not None:
        session_service.issue_with_id(
            db, user, mine, created_at=mine_row.created_at, expires_at=mine_row.expires_at
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
