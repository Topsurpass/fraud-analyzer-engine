"""Logging in, logging out, and reporting who is signed in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.db.app_state import get_session
from app.errors import AppError, ErrorCode
from app.models.user import User
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
    auth_service.change_password(db, user, body.current_password, body.new_password)

    # Every session but this one. Being signed out of the browser you just used
    # to change your password is a bug; leaving an intercepted session alive is
    # a hole.
    mine = session_service.digest(_bearer(request))
    session_service.revoke_all_for_user(db, user.id)
    session_service.issue_with_id(db, user, mine)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
