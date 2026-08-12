"""Job schema and status enum for the background processing queue.

State machine
-------------
::

    PENDING ──→ PROCESSING ──→ COMPLETED
       ↑              │
       └──────────────┘  (retry: count < max)
                      │
                      └──→ FAILED  (retries exhausted)

- PENDING → PROCESSING: claimed by the worker via ``claim_next_pending_job``.
- PROCESSING → COMPLETED: pipeline finished successfully.
- PROCESSING → PENDING: pipeline raised, but ``retry_count < max_retries``.
- PROCESSING → FAILED: pipeline raised and retries are exhausted.

FAILED is terminal — no further transitions.
"""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Allowed job states (string enum for JSON serialisation)."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(BaseModel):
    """A single processing job."""

    id: UUID
    status: JobStatus = JobStatus.PENDING
    owner_user_id: UUID | None = None
    input_filename: str
    input_path: str
    output_docx_path: str | None = None
    output_pptx_path: str | None = None
    error_message: str | None = None
    retry_count: int = Field(default=0)
    max_retries: int = Field(default=2)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
