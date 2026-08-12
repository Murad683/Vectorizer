"""Initial schema: jobs and users tables.

Revision ID: 0001
Revises:
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs (
            id                UUID PRIMARY KEY,
            status            TEXT NOT NULL DEFAULT 'pending',
            owner_user_id     UUID,
            input_filename    TEXT NOT NULL,
            input_path        TEXT NOT NULL,
            output_docx_path  TEXT,
            output_pptx_path  TEXT,
            error_message     TEXT,
            retry_count       INTEGER NOT NULL DEFAULT 0,
            max_retries       INTEGER NOT NULL DEFAULT 2,
            created_at        TIMESTAMPTZ NOT NULL,
            updated_at        TIMESTAMPTZ NOT NULL
        )
        """)
    op.execute("CREATE INDEX ix_jobs_status_created_at ON jobs (status, created_at)")
    op.execute("CREATE INDEX ix_jobs_owner_user_id ON jobs (owner_user_id)")

    op.execute("""
        CREATE TABLE users (
            id                UUID PRIMARY KEY,
            email             TEXT NOT NULL UNIQUE,
            hashed_password   TEXT NOT NULL,
            role              TEXT NOT NULL DEFAULT 'member',
            is_active         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ NOT NULL
        )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP TABLE IF EXISTS users")
