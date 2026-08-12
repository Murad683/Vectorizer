"""PostgreSQL-backed job persistence.

Uses psycopg (v3) directly with hand-written SQL — no ORM. This matches
the project's existing style (hand-rolled SQL in the SQLite-era stores,
Pydantic models for validation, no SQLAlchemy elsewhere in the
codebase) and keeps the footprint small for two simple tables. Schema
is managed by Alembic migrations (``backend/migrations/``), not ad-hoc
``CREATE TABLE IF NOT EXISTS`` calls here.

``claim_next_pending_job`` uses Postgres's ``SELECT ... FOR UPDATE SKIP
LOCKED`` — the standard job-queue claim pattern — inside a single
``UPDATE ... FROM ... RETURNING`` statement. A single SQL statement is
atomic on its own, so no explicit multi-statement transaction/locking
is needed; the connection runs in autocommit mode throughout, same as
every other method here.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import DictRow, dict_row

from app.core.queue.models import Job, JobStatus

logger = logging.getLogger(__name__)


def _row_to_job(row: DictRow) -> Job:
    return Job(
        id=row["id"],
        status=JobStatus(row["status"]),
        owner_user_id=row["owner_user_id"],
        input_filename=row["input_filename"],
        input_path=row["input_path"],
        output_docx_path=row["output_docx_path"],
        output_pptx_path=row["output_pptx_path"],
        error_message=row["error_message"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class JobStore:
    """Synchronous, psycopg-backed job store."""

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self._conn.execute("SET TIME ZONE 'UTC'")

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def create_job(
        self,
        *,
        job_id: UUID,
        input_filename: str,
        input_path: str,
        owner_user_id: UUID | None = None,
    ) -> Job:
        """Insert a new PENDING job and return it."""
        now = datetime.now(timezone.utc)
        row = self._conn.execute(
            """
            INSERT INTO jobs
                (id, status, owner_user_id, input_filename, input_path,
                 retry_count, max_retries, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 0, 2, %s, %s)
            RETURNING *
            """,
            (
                job_id,
                JobStatus.PENDING.value,
                owner_user_id,
                input_filename,
                input_path,
                now,
                now,
            ),
        ).fetchone()
        assert row is not None
        return _row_to_job(row)

    def get_job(self, job_id: UUID) -> Job | None:
        """Return the job with the given id, or None."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row is not None else None

    def list_jobs(
        self,
        status: JobStatus | None = None,
        owner_user_id: UUID | None = None,
    ) -> list[Job]:
        """Return jobs, optionally filtered by status and/or owner, newest first."""
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = %s")
            params.append(status.value)
        if owner_user_id is not None:
            conditions.append("owner_user_id = %s")
            params.append(owner_user_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC",  # noqa: S608
            params,
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def delete_job(self, job_id: UUID) -> bool:
        """Delete a job row; return True if a row was deleted."""
        cursor = self._conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        return cursor.rowcount > 0

    def update_job(self, job_id: UUID, **fields: Any) -> Job | None:
        """Update arbitrary fields on a job; returns the updated job."""
        if not fields:
            return self.get_job(job_id)
        fields["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values: list[Any] = [
            v.value if isinstance(v, JobStatus) else v for v in fields.values()
        ]
        values.append(job_id)
        row = self._conn.execute(
            f"UPDATE jobs SET {set_clause} WHERE id = %s RETURNING *",  # noqa: S608
            values,
        ).fetchone()
        return _row_to_job(row) if row is not None else None

    def claim_next_pending_job(self) -> Job | None:
        """Atomically claim the oldest PENDING job, or return None.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a CTE feeding
        an ``UPDATE ... FROM ... RETURNING`` — concurrent workers each
        skip rows already locked by another worker's in-flight claim
        instead of blocking on them, so the first writer wins and the
        rest immediately move on to the next-oldest unlocked PENDING row
        (or return None if there isn't one).
        """
        row = self._conn.execute(
            """
            WITH next_job AS (
                SELECT id FROM jobs
                WHERE status = %s
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs
            SET status = %s, updated_at = %s
            FROM next_job
            WHERE jobs.id = next_job.id
            RETURNING jobs.*
            """,
            (
                JobStatus.PENDING.value,
                JobStatus.PROCESSING.value,
                datetime.now(timezone.utc),
            ),
        ).fetchone()
        return _row_to_job(row) if row is not None else None
