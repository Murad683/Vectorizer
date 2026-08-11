"""FastAPI application entrypoint for the Test Material Vectorizer backend."""

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.health import router as health_router
from app.config import get_settings
from app.logging_config import configure_logging
from app.middleware.request_id import RequestIDMiddleware
from app.models.errors import ErrorResponse

logger = logging.getLogger(__name__)


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
    )

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    return app


app = create_app()
