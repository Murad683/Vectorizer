"""Authentication routes.

- ``POST /auth/login``: email + password → short-lived JWT access token
  (response body) + an httpOnly refresh-token cookie.
- ``POST /auth/refresh``: reads the refresh cookie, rotates it, and
  issues a new access token. This is what lets a page reload survive
  without re-login even though the access token itself lives only in
  frontend memory (never localStorage — see Phase 8's decision, which
  this phase does not reverse).
- ``POST /auth/logout``: revokes the refresh token server-side and
  clears the cookie.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.core.auth.models import Role
from app.core.auth.security import (
    REFRESH_TOKEN_LIFETIME,
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.core.auth.store import RefreshTokenStore, UserStore
from app.models.errors import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE_NAME = "refresh_token"


class LoginRequest(BaseModel):
    """Email + password login payload."""

    email: str
    password: str


def _set_refresh_cookie(response: JSONResponse, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path="/",
    )


def _clear_refresh_cookie(response: JSONResponse) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE_NAME, path="/")


def _issue_session(
    user_id: UUID, role: Role, email: str, refresh_store: RefreshTokenStore
) -> tuple[str, str]:
    """Create a new access token + rotated refresh token pair.

    Returns ``(access_token, raw_refresh_token)``.
    """
    access_token = create_access_token(user_id, role, email)
    raw_refresh, hashed_refresh = generate_refresh_token()
    refresh_store.create(
        token_id=uuid4(),
        user_id=user_id,
        token_hash=hashed_refresh,
        expires_at=datetime.now(timezone.utc) + REFRESH_TOKEN_LIFETIME,
    )
    return access_token, raw_refresh


def _unauthorized(request: Request, error_code: str, message: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    response = JSONResponse(
        status_code=401,
        content=ErrorResponse(
            error_code=error_code, message=message, request_id=request_id
        ).model_dump(),
    )
    _clear_refresh_cookie(response)
    return response


@router.post("/login", response_model=None)
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Verify credentials, return a JWT access token, set the refresh cookie.

    Returns the same generic 401 whether the email doesn't exist or the
    password is wrong — no information leakage.
    """
    store: UserStore = request.app.state.user_store
    refresh_store: RefreshTokenStore = request.app.state.refresh_token_store
    user = store.get_user_by_email(body.email)

    if (
        user is None
        or not verify_password(body.password, user.hashed_password)
        or not user.is_active
    ):
        return _unauthorized(
            request, "invalid_credentials", "Invalid email or password."
        )

    access_token, raw_refresh = _issue_session(
        user.id, user.role, user.email, refresh_store
    )
    logger.info("User %s logged in", user.email)
    response = JSONResponse(
        content={"access_token": access_token, "token_type": "bearer"}
    )
    _set_refresh_cookie(response, raw_refresh)
    return response


@router.post("/refresh", response_model=None)
async def refresh(request: Request) -> JSONResponse:
    """Rotate the refresh token and issue a new access token.

    401 (and the cookie cleared) on a missing, invalid, expired, revoked,
    or tampered refresh token, or if the owning user was deactivated
    since the token was issued.
    """
    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if not raw_token:
        return _unauthorized(
            request, "invalid_refresh_token", "No refresh token provided."
        )

    refresh_store: RefreshTokenStore = request.app.state.refresh_token_store
    user_store: UserStore = request.app.state.user_store

    token_hash = hash_refresh_token(raw_token)
    stored = refresh_store.get_valid_by_hash(token_hash)
    if stored is None:
        return _unauthorized(
            request, "invalid_refresh_token", "Refresh token is invalid or expired."
        )

    user = user_store.get_user_by_id(stored.user_id)
    if user is None or not user.is_active:
        refresh_store.revoke_by_hash(token_hash)
        return _unauthorized(
            request, "invalid_refresh_token", "Refresh token is invalid or expired."
        )

    # Rotate: invalidate the token that was just used, issue a fresh pair.
    refresh_store.revoke_by_hash(token_hash)
    access_token, new_raw_refresh = _issue_session(
        user.id, user.role, user.email, refresh_store
    )

    response = JSONResponse(
        content={"access_token": access_token, "token_type": "bearer"}
    )
    _set_refresh_cookie(response, new_raw_refresh)
    return response


@router.post("/logout", response_model=None)
async def logout(request: Request) -> JSONResponse:
    """Revoke the refresh token server-side and clear the cookie.

    Doesn't require a valid access token — logout must work even after
    the (short-lived) access token has already expired.
    """
    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_token:
        refresh_store: RefreshTokenStore = request.app.state.refresh_token_store
        refresh_store.revoke_by_hash(hash_refresh_token(raw_token))

    response = JSONResponse(content={"ok": True})
    _clear_refresh_cookie(response)
    return response
