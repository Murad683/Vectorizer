"""Add refresh_tokens table for the httpOnly-cookie refresh flow.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE refresh_tokens (
            id           UUID PRIMARY KEY,
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE,
            issued_at    TIMESTAMPTZ NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            revoked      BOOLEAN NOT NULL DEFAULT FALSE
        )
        """)
    op.execute(
        "CREATE INDEX ix_refresh_tokens_token_hash ON refresh_tokens (token_hash)"
    )
    op.execute("CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
