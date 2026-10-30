"""Add per-job Gemini call counter and the job submission log.

``jobs.vision_calls`` carries the number of Gemini requests a job has used
across its retries, so the per-job ceiling cannot be restarted by a retry.
``job_submissions`` is an insert-only log used for the daily upload limit;
it is separate from ``jobs`` so deleting a job does not give the quota back.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN vision_calls INTEGER NOT NULL DEFAULT 0")
    op.execute("""
        CREATE TABLE job_submissions (
            id             UUID PRIMARY KEY,
            owner_user_id  UUID NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL
        )
        """)
    op.execute(
        "CREATE INDEX ix_job_submissions_owner_created "
        "ON job_submissions (owner_user_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_submissions")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS vision_calls")
