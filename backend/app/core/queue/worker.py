"""Background worker that processes queued jobs sequentially.

Architecture choice: an ``asyncio.Task`` started at application startup,
polling the SQLite store on a fixed interval. Reasoning:

- No external broker (Redis/Celery) to operate — a single-process
  in-memory loop is the simplest correct implementation for the MVP
  volume (1 500–3 000 pages/month).
- The pipeline itself is synchronous (blocking Gemini calls, file I/O),
  so the worker runs it in a thread via ``asyncio.to_thread`` to avoid
  blocking the FastAPI event loop.
- Sequential processing (one job at a time) — concurrency can be added
  later once the single-job path is proven correct.
"""

import asyncio
import logging
import traceback
from pathlib import Path

from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.ingestion import get_extractor
from app.core.queue.models import JobStatus
from app.core.queue.store import JobStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0
_JOBS_OUTPUT_DIR = Path("data/jobs")


def _process_job_sync(input_path: str, output_dir: Path) -> tuple[str, str]:
    """Run the full pipeline synchronously; return (docx_path, pptx_path).

    This is the SAME pipeline as ``scripts/assemble_document.py`` —
    extract → classify → assemble, with vision called inside the
    assemblers for DIAGRAM elements. No pipeline code is modified.
    """
    source = Path(input_path)
    classified = classify_elements(get_extractor(source).extract(source))

    output_dir.mkdir(parents=True, exist_ok=True)

    document = assemble_docx(classified)
    docx_path = output_dir / "output.docx"
    document.save(str(docx_path))

    prs = assemble_pptx(classified)
    pptx_path = output_dir / "output.pptx"
    prs.save(str(pptx_path))

    return str(docx_path), str(pptx_path)


async def _worker_loop(store: JobStore) -> None:
    """Poll for pending jobs and process them, forever."""
    while True:
        job = store.claim_next_pending_job()
        if job is None:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            continue

        logger.info("Processing job %s (%s)", job.id, job.input_filename)
        output_dir = _JOBS_OUTPUT_DIR / str(job.id)

        try:
            docx_path, pptx_path = await asyncio.to_thread(
                _process_job_sync, job.input_path, output_dir
            )
            store.update_job(
                job.id,
                status=JobStatus.COMPLETED,
                output_docx_path=docx_path,
                output_pptx_path=pptx_path,
                error_message=None,
            )
            logger.info("Job %s completed", job.id)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            job = store.get_job(job.id)
            assert job is not None
            if job.retry_count < job.max_retries:
                store.update_job(
                    job.id,
                    status=JobStatus.PENDING,
                    retry_count=job.retry_count + 1,
                    error_message=error_msg,
                )
                logger.warning(
                    "Job %s failed (attempt %d/%d), will retry: %s",
                    job.id,
                    job.retry_count + 1,
                    job.max_retries,
                    exc,
                )
            else:
                store.update_job(
                    job.id,
                    status=JobStatus.FAILED,
                    error_message=error_msg,
                )
                logger.error("Job %s failed permanently: %s", job.id, exc)


async def start_worker(store: JobStore) -> asyncio.Task[None]:
    """Launch the worker loop as a background asyncio task."""
    task = asyncio.create_task(_worker_loop(store))
    logger.info("Background worker started")
    return task
