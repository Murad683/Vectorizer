"""FastAPI application entrypoint for the Test Material Vectorizer backend."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.users import router as users_router
from app.config import get_settings
from app.core.auth.dependencies import _AuthError
from app.core.auth.models import Role
from app.core.auth.security import hash_password
from app.core.auth.store import UserStore
from app.core.queue.store import JobStore
from app.core.queue.worker import start_worker
from app.logging_config import configure_logging
from app.middleware.request_id import RequestIDMiddleware
from app.models.errors import ErrorResponse

logger = logging.getLogger(__name__)


def _bootstrap_admin(user_store: UserStore) -> None:
    """Create the initial admin user from env vars if none exists yet."""
    if user_store.has_any_admin():
        return
    settings = get_settings()
    if not settings.admin_email or not settings.admin_password:
        logger.warning(
            "No admin user exists and ADMIN_EMAIL/ADMIN_PASSWORD are not set — "
            "skipping admin bootstrap. Set them in .env to create the first admin."
        )
        return
    user_store.create_user(
        user_id=uuid4(),
        email=settings.admin_email,
        hashed_password=hash_password(settings.admin_password),
        role=Role.ADMIN,
    )
    logger.info("Bootstrap admin user created (%s)", settings.admin_email)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the stores and background worker lifecycle."""
    settings = get_settings()

    job_store = JobStore(dsn=settings.database_url)
    user_store = UserStore(dsn=settings.database_url)
    _bootstrap_admin(user_store)

    app.state.job_store = job_store
    app.state.user_store = user_store
    worker_task = await start_worker(job_store)
    yield
    worker_task.cancel()
    job_store.close()
    user_store.close()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Test Material Vectorizer API",
        description=(
            "Converts pixelated PDF/DOCX test materials (Azerbaijani educational "
            "content — algorithm flowcharts, tables, questions) into clean "
            "vector-quality Word/PowerPoint files."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(_AuthError)
    async def auth_error_handler(
        request: Request, exc: _AuthError  # noqa: ARG001
    ) -> JSONResponse:
        """Return the pre-built auth error response."""
        return exc.response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Return a consistent JSON error shape instead of a raw traceback."""
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled exception while processing request")
        error = ErrorResponse(
            error_code="internal_server_error",
            message="An unexpected error occurred. Please try again later.",
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content=error.model_dump())

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(jobs_router)
    return app


app = create_app()
