"""Add the page selection columns of a job.

A scanned PDF waits in the status ``awaiting_selection`` until its owner has
picked the pages to read (``status`` is plain TEXT, so the new status needs no
schema change). ``page_count`` is the number of pages, ``selected_pages`` the
chosen ones as ``"1-3,7"`` (``NULL`` = no selection, process everything;
``""`` = none chosen), ``keep_unselected`` says whether the other pages stay in
the result as plain images.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN page_count INTEGER")
    op.execute("ALTER TABLE jobs ADD COLUMN selected_pages TEXT")
    op.execute("ALTER TABLE jobs ADD COLUMN keep_unselected BOOLEAN NOT NULL DEFAULT TRUE")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS keep_unselected")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS selected_pages")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS page_count")
