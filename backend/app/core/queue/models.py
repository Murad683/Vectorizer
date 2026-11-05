"""Job schema and status enum for the background processing queue.

State machine
-------------
::

    AWAITING_SELECTION ──→ PENDING   (scanned PDF: the owner picked the pages)
    AWAITING_SELECTION ──→ FAILED    (nobody picked in time)
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

    AWAITING_SELECTION = "awaiting_selection"  # a scanned PDF: pages to be chosen
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
    vision_calls: int = Field(default=0)
    page_count: int | None = None
    # "1-3,7"; None = no selection (everything), "" = no page chosen.
    selected_pages: str | None = None
    keep_unselected: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
