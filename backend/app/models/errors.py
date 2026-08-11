"""Unified error response schema used across the entire application."""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Consistent JSON shape for every error returned by the API."""

    error_code: str = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable description of the error.")
    request_id: str | None = Field(
        default=None,
        description="Correlation id of the request that produced the error.",
    )
