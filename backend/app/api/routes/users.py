"""User management routes (admin-only): create, list, deactivate, delete users."""

import logging
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.core.auth.dependencies import require_admin
from app.core.auth.models import Role, User
from app.core.auth.security import hash_password
from app.core.auth.store import UserStore
from app.core.queue.store import JobStore
from app.models.errors import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class CreateUserRequest(BaseModel):
    """Payload for creating a new user."""

    email: str
    password: str
    role: Role = Role.MEMBER


class UpdateUserRequest(BaseModel):
    """Payload for updating a user's active status."""

    is_active: bool


def _user_response(user: User, job_count: int) -> dict:
    """Serialise a User for the API response — never includes the hash.

    ``job_count`` lets the frontend proactively disable the delete
    action (only permitted at zero) without a separate request.
    """
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "job_count": job_count,
    }


def _error(
    status_code: int, error_code: str, message: str, request: Request
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error_code=error_code, message=message, request_id=request_id
        ).model_dump(),
    )


@router.post("", response_model=None)
async def create_user(
    body: CreateUserRequest,
    request: Request,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    """Create a new user account (admin-only)."""
    store: UserStore = request.app.state.user_store

    try:
        user = store.create_user(
            user_id=uuid4(),
            email=body.email,
            hashed_password=await run_in_threadpool(hash_password, body.password),
            role=body.role,
        )
    except psycopg.errors.UniqueViolation:
        return _error(
            409,
            "email_already_exists",
            f"A user with email '{body.email}' already exists.",
            request,
        )

    logger.info("Admin created user %s (role=%s)", user.email, user.role.value)
    return JSONResponse(status_code=201, content=_user_response(user, job_count=0))


@router.get("", response_model=None)
async def list_users(
    request: Request,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    """List all users (admin-only). Never includes password hashes."""
    store: UserStore = request.app.state.user_store
    job_store: JobStore = request.app.state.job_store
    users = store.list_users()
    return JSONResponse(
        content=[
            _user_response(u, job_count=len(job_store.list_jobs(owner_user_id=u.id)))
            for u in users
        ]
    )


@router.patch("/{user_id}", response_model=None)
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    request: Request,
    admin: User = Depends(require_admin),
) -> JSONResponse:
    """Activate or deactivate a user (admin-only).

    Deactivating blocks future logins and invalidates any live session
    on the next authenticated request (both check ``is_active``) without
    touching the user's job history. An admin cannot deactivate their
    own account.
    """
    if user_id == admin.id:
        return _error(
            400,
            "cannot_modify_self",
            "You cannot deactivate your own account.",
            request,
        )

    store: UserStore = request.app.state.user_store
    target = store.get_user_by_id(user_id)
    if target is None:
        return _error(404, "user_not_found", f"No user with id {user_id}.", request)

    updated = store.update_user(user_id, is_active=body.is_active)
    assert updated is not None
    logger.info(
        "Admin %s set is_active=%s for user %s",
        admin.email,
        body.is_active,
        updated.email,
    )
    job_store: JobStore = request.app.state.job_store
    job_count = len(job_store.list_jobs(owner_user_id=user_id))
    return JSONResponse(content=_user_response(updated, job_count=job_count))


@router.delete("/{user_id}", response_model=None, status_code=204)
async def delete_user(
    user_id: UUID,
    request: Request,
    admin: User = Depends(require_admin),
) -> Response | JSONResponse:
    """Permanently delete a user (admin-only).

    Only permitted if the user owns zero jobs — otherwise 409, since
    deleting them would orphan their job history; deactivate instead.
    An admin cannot delete their own account.
    """
    if user_id == admin.id:
        return _error(
            400, "cannot_modify_self", "You cannot delete your own account.", request
        )

    user_store: UserStore = request.app.state.user_store
    target = user_store.get_user_by_id(user_id)
    if target is None:
        return _error(404, "user_not_found", f"No user with id {user_id}.", request)

    job_store: JobStore = request.app.state.job_store
    owned_jobs = job_store.list_jobs(owner_user_id=user_id)
    if owned_jobs:
        return _error(
            409,
            "user_owns_jobs",
            f"Cannot delete {target.email}: they own {len(owned_jobs)} job(s). "
            "Deactivate the account instead.",
            request,
        )

    user_store.delete_user(user_id)
    logger.info("Admin %s deleted user %s", admin.email, target.email)
    return Response(status_code=204)
