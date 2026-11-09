"""Add the quality log and the request kinds of the usage log.

``job_quality`` is an insert-only record of what the automatic gap check found
on one finished job: how many pages were checked, how many errors and pages to
review, and how many of each kind. Numbers only (no file names, no text) and no
foreign keys, like ``job_usage``: deleting a job keeps the history.

``job_usage.calls_by_kind`` counts the Gemini requests of one attempt by kind
(page, page_repeat, flowchart_rescue, ...). It is empty for older rows.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE job_quality (
            id              UUID PRIMARY KEY,
            job_id          UUID NOT NULL,
            owner_user_id   UUID,
            created_at      TIMESTAMPTZ NOT NULL,
            pages_checked   INTEGER NOT NULL,
            errors          INTEGER NOT NULL,
            reviews         INTEGER NOT NULL,
            kinds           JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """)
    op.execute("CREATE INDEX ix_job_quality_created ON job_quality (created_at)")
    op.execute("ALTER TABLE job_usage ADD COLUMN calls_by_kind JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE job_usage DROP COLUMN IF EXISTS calls_by_kind")
    op.execute("DROP TABLE IF EXISTS job_quality")
