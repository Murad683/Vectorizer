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
        vision_calls=row["vision_calls"],
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
        if owner_user_id is not None:
            # Insert-only log behind the daily limit: unlike ``jobs`` rows it
            # survives ``delete_job``.
            self._conn.execute(
                "INSERT INTO job_submissions (id, owner_user_id, created_at) "
                "VALUES (%s, %s, %s)",
                (job_id, owner_user_id, now),
            )
        return _row_to_job(row)

    def list_finished_jobs_before(self, cutoff: datetime) -> list[Job]:
        """Return COMPLETED/FAILED jobs last updated before ``cutoff``."""
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status = ANY(%s) AND updated_at < %s "
            "ORDER BY updated_at",
            ([JobStatus.COMPLETED.value, JobStatus.FAILED.value], cutoff),
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def count_active_jobs(self, owner_user_id: UUID) -> int:
        """Return how many of the user's jobs are PENDING or PROCESSING."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM jobs "
            "WHERE owner_user_id = %s AND status = ANY(%s)",
            (
                owner_user_id,
                [JobStatus.PENDING.value, JobStatus.PROCESSING.value],
            ),
        ).fetchone()
        assert row is not None
        return int(row["c"])

    def count_submissions_since(self, owner_user_id: UUID, since: datetime) -> int:
        """Return how many jobs the user submitted at or after ``since``."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM job_submissions "
            "WHERE owner_user_id = %s AND created_at >= %s",
            (owner_user_id, since),
        ).fetchone()
        assert row is not None
        return int(row["c"])

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
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Job]:
        """Return jobs, optionally filtered/paginated, newest first.

        ``search`` does a case-insensitive substring match (``ILIKE``)
        against ``input_filename``.
        """
        where, params = self._filter_clause(status, owner_user_id, search)
        query = f"SELECT * FROM jobs {where} ORDER BY created_at DESC"  # noqa: S608
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        if offset is not None:
            query += " OFFSET %s"
            params.append(offset)
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_job(r) for r in rows]

    def count_jobs(
        self,
        status: JobStatus | None = None,
        owner_user_id: UUID | None = None,
        search: str | None = None,
    ) -> int:
        """Return the total count of jobs matching the given filters."""
        where, params = self._filter_clause(status, owner_user_id, search)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM jobs {where}",  # noqa: S608
            params,
        ).fetchone()
        assert row is not None
        return int(row["c"])

    @staticmethod
    def _filter_clause(
        status: JobStatus | None,
        owner_user_id: UUID | None,
        search: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = %s")
            params.append(status.value)
        if owner_user_id is not None:
            conditions.append("owner_user_id = %s")
            params.append(owner_user_id)
        if search:
            conditions.append("input_filename ILIKE %s")
            params.append(f"%{search}%")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

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

    def recover_interrupted_jobs(self) -> int:
        """Re-queue jobs a previous process left in PROCESSING; return how many.

        Only one worker runs, so at startup nothing can legitimately be
        PROCESSING: those jobs were cut off by a restart or crash. Left alone
        they would never finish and would hold one of the user's active-job
        slots forever. The interrupted run counts as an attempt, so a job
        that keeps killing the process ends up FAILED instead of looping.
        """
        now = datetime.now(timezone.utc)
        self._conn.execute(
            """
            UPDATE jobs
            SET status = %s, error_message = %s, updated_at = %s
            WHERE status = %s AND retry_count >= max_retries
            """,
            (
                JobStatus.FAILED.value,
                "Server yenidən başladığı üçün iş yarımçıq qaldı.",
                now,
                JobStatus.PROCESSING.value,
            ),
        )
        cursor = self._conn.execute(
            """
            UPDATE jobs
            SET status = %s, retry_count = retry_count + 1, updated_at = %s
            WHERE status = %s
            """,
            (JobStatus.PENDING.value, now, JobStatus.PROCESSING.value),
        )
        return cursor.rowcount

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
