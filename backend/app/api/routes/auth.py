"""Authentication routes: login to receive a JWT access token."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.auth.security import create_access_token, verify_password
from app.core.auth.store import UserStore
from app.models.errors import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Email + password login payload."""

    email: str
    password: str


@router.post("/login", response_model=None)
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Verify credentials and return a JWT access token.

    Returns the same generic 401 whether the email doesn't exist or the
    password is wrong — no information leakage.
    """
    store: UserStore = request.app.state.user_store
    user = store.get_user_by_email(body.email)

    if user is None or not verify_password(body.password, user.hashed_password):
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error_code="invalid_credentials",
                message="Invalid email or password.",
                request_id=request_id,
            ).model_dump(),
        )

    if not user.is_active:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error_code="invalid_credentials",
                message="Invalid email or password.",
                request_id=request_id,
            ).model_dump(),
        )

    token = create_access_token(user.id, user.role)
    logger.info("User %s logged in", user.email)
    return JSONResponse(
        content={
            "access_token": token,
            "token_type": "bearer",
        }
    )
