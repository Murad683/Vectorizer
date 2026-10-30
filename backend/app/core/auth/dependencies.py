"""FastAPI dependencies for authentication and authorization."""

from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.auth.models import Role, User
from app.core.auth.security import decode_access_token
from app.core.auth.store import UserStore
from app.models.errors import ErrorResponse

_bearer = HTTPBearer(auto_error=False)


def _get_user_store(request: Request) -> UserStore:
    return request.app.state.user_store  # type: ignore[no-any-return]


def _error_response(status_code: int, error_code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error_code=error_code, message=message).model_dump(),
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Extract and verify the JWT, confirm the user still exists and is active.

    Raises 401 (via JSONResponse) on missing/invalid/expired token or
    deactivated user.
    """
    if credentials is None:
        raise _AuthError(
            _error_response(401, "not_authenticated", "Missing authorization token.")
        )

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise _AuthError(
            _error_response(401, "invalid_token", "Token is invalid or expired.")
        ) from None

    user_id = UUID(payload["sub"])
    store = _get_user_store(request)
    user = store.get_user_by_id(user_id)

    if user is None or not user.is_active:
        raise _AuthError(
            _error_response(401, "invalid_token", "Token is invalid or expired.")
        )

    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require the authenticated user to have the ADMIN role.

    Raises 403 if the user is not an admin.
    """
    if user.role != Role.ADMIN:
        raise _AuthError(_error_response(403, "forbidden", "Admin access required."))
    return user


class _AuthError(Exception):
    """Wraps a JSONResponse so the dependency can short-circuit the request."""

    def __init__(self, response: JSONResponse) -> None:
        self.response = response
