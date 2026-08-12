"""Tests for the background job queue: store, worker retry logic, and API."""

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore


@pytest.fixture()
def store(job_store: JobStore) -> JobStore:
    """Alias for the shared ``job_store`` fixture (see conftest.py)."""
    return job_store


@pytest.fixture()
def pending_job(store: JobStore, sample_docx: Path) -> Job:
    """Create a PENDING job pointing at the real sample DOCX."""
    return store.create_job(
        job_id=uuid4(),
        input_filename="test.docx",
        input_path=str(sample_docx),
    )


class TestJobStore:
    def test_create_and_get(self, store: JobStore, sample_docx: Path) -> None:
        job_id = uuid4()
        job = store.create_job(
            job_id=job_id,
            input_filename="my_file.docx",
            input_path=str(sample_docx),
        )
        assert job.id == job_id
        assert job.status == JobStatus.PENDING
        assert job.input_filename == "my_file.docx"

        fetched = store.get_job(job_id)
        assert fetched is not None
        assert fetched.id == job_id

    def test_get_nonexistent_returns_none(self, store: JobStore) -> None:
        assert store.get_job(uuid4()) is None

    def test_list_jobs_all(self, store: JobStore, sample_docx: Path) -> None:
        store.create_job(
            job_id=uuid4(), input_filename="a.docx", input_path=str(sample_docx)
        )
        store.create_job(
            job_id=uuid4(), input_filename="b.docx", input_path=str(sample_docx)
        )
        assert len(store.list_jobs()) == 2

    def test_list_jobs_by_status(self, store: JobStore, sample_docx: Path) -> None:
        j1 = store.create_job(
            job_id=uuid4(), input_filename="a.docx", input_path=str(sample_docx)
        )
        store.create_job(
            job_id=uuid4(), input_filename="b.docx", input_path=str(sample_docx)
        )
        store.update_job(j1.id, status=JobStatus.PROCESSING)
        assert len(store.list_jobs(status=JobStatus.PENDING)) == 1
        assert len(store.list_jobs(status=JobStatus.PROCESSING)) == 1

    def test_update_job(self, store: JobStore, pending_job: Job) -> None:
        updated = store.update_job(
            pending_job.id,
            status=JobStatus.COMPLETED,
            output_docx_path="/out/file.docx",
        )
        assert updated is not None
        assert updated.status == JobStatus.COMPLETED
        assert updated.output_docx_path == "/out/file.docx"

    def test_claim_next_pending_job(self, store: JobStore, pending_job: Job) -> None:
        claimed = store.claim_next_pending_job()
        assert claimed is not None
        assert claimed.id == pending_job.id
        assert claimed.status == JobStatus.PROCESSING
        # No more pending jobs to claim.
        assert store.claim_next_pending_job() is None

    def test_claim_returns_oldest_first(
        self, store: JobStore, sample_docx: Path
    ) -> None:
        j1 = store.create_job(
            job_id=uuid4(), input_filename="first.docx", input_path=str(sample_docx)
        )
        time.sleep(0.01)
        store.create_job(
            job_id=uuid4(), input_filename="second.docx", input_path=str(sample_docx)
        )
        claimed = store.claim_next_pending_job()
        assert claimed is not None
        assert claimed.id == j1.id

    def test_concurrent_claims_never_double_claim(
        self, job_store: JobStore, sample_docx: Path
    ) -> None:
        """Real concurrency test for SKIP LOCKED: N jobs, N workers racing
        to claim, each job claimed by exactly one worker.

        Depends on ``job_store`` (unused directly) purely to trigger the
        per-test table truncation — this test opens its own independent
        connections per simulated worker rather than using it.
        """
        from tests.conftest import TEST_DATABASE_URL

        seed_store = JobStore(dsn=TEST_DATABASE_URL)
        job_ids = [
            seed_store.create_job(
                job_id=uuid4(),
                input_filename=f"job{i}.docx",
                input_path=str(sample_docx),
            ).id
            for i in range(8)
        ]
        seed_store.close()

        results: list[UUID | None] = [None] * 8
        lock = threading.Lock()

        def worker(index: int) -> None:
            worker_store = JobStore(dsn=TEST_DATABASE_URL)
            claimed = worker_store.claim_next_pending_job()
            with lock:
                results[index] = claimed.id if claimed else None
            worker_store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        claimed_ids = [r for r in results if r is not None]
        assert len(claimed_ids) == 8, "every worker should have claimed a job"
        assert len(set(claimed_ids)) == 8, "no job claimed by more than one worker"
        assert set(claimed_ids) == set(job_ids)


class TestWorkerRetry:
    def test_retry_then_fail(self, store: JobStore, pending_job: Job) -> None:
        """Simulate the worker's retry logic: fail twice, then permanently."""
        for attempt in range(pending_job.max_retries + 1):
            claimed = store.claim_next_pending_job()
            assert claimed is not None
            assert claimed.status == JobStatus.PROCESSING
            # Simulate failure.
            job = store.get_job(claimed.id)
            assert job is not None
            if job.retry_count < job.max_retries:
                store.update_job(
                    job.id,
                    status=JobStatus.PENDING,
                    retry_count=job.retry_count + 1,
                    error_message=f"fail attempt {attempt}",
                )
            else:
                store.update_job(
                    job.id,
                    status=JobStatus.FAILED,
                    error_message=f"permanent failure attempt {attempt}",
                )

        final = store.get_job(pending_job.id)
        assert final is not None
        assert final.status == JobStatus.FAILED
        assert final.retry_count == pending_job.max_retries
        assert final.error_message is not None
        # No more jobs to pick up.
        assert store.claim_next_pending_job() is None


class TestWorkerPipelineFailure:
    def test_pipeline_exception_retries_then_fails(
        self, store: JobStore, pending_job: Job
    ) -> None:
        """Mock the pipeline to raise; verify retry + eventual FAILED status."""
        with patch(
            "app.core.queue.worker._process_job_sync",
            side_effect=RuntimeError("Gemini exploded"),
        ):
            # Run the worker loop for just long enough to process retries.
            from app.core.queue.worker import _worker_loop

            async def run_briefly() -> None:
                task = asyncio.create_task(_worker_loop(store))
                # Wait for retries to complete (max_retries=2 means 3 total attempts).
                for _ in range(30):
                    await asyncio.sleep(0.3)
                    job = store.get_job(pending_job.id)
                    if job and job.status == JobStatus.FAILED:
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(run_briefly())

        final = store.get_job(pending_job.id)
        assert final is not None
        assert final.status == JobStatus.FAILED
        assert "Gemini exploded" in (final.error_message or "")
        assert final.retry_count == final.max_retries
