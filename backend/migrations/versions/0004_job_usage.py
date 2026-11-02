"""Add the job usage log.

``job_usage`` is an insert-only record of what one processing attempt used:
pages, Gemini requests, tokens, estimated cost, duration and outcome. It holds
numbers only (no file names, no text) and, like ``job_submissions``, has no
foreign keys: deleting a job or a user keeps the cost history.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE job_usage (
            id              UUID PRIMARY KEY,
            job_id          UUID NOT NULL,
            owner_user_id   UUID,
            created_at      TIMESTAMPTZ NOT NULL,
            file_ext        TEXT NOT NULL,
            pages           INTEGER,
            vision_calls    INTEGER NOT NULL,
            input_tokens    BIGINT NOT NULL,
            output_tokens   BIGINT NOT NULL,
            thought_tokens  BIGINT NOT NULL,
            est_cost_usd    NUMERIC(10, 5) NOT NULL,
            duration_s      NUMERIC(8, 1) NOT NULL,
            outcome         TEXT NOT NULL
        )
        """)
    op.execute("CREATE INDEX ix_job_usage_created ON job_usage (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_usage")
