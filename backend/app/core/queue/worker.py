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

from app.config import get_settings
from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.ingestion import get_extractor
from app.core.queue.models import JobStatus
from app.core.queue.store import JobStore
from app.core.vision import VisionBudget, VisionBudgetExceededError, get_vision_provider
from app.core.vision.cache import CachedVisionProvider

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0
_JOBS_OUTPUT_DIR = Path("data/jobs")


_BUDGET_EXCEEDED_MESSAGE = (
    "Bu iş üçün icazə verilən süni intellekt sorğularının sayı aşıldı. "
    "Faylı bölüb daha kiçik hissələrlə yenidən yükləyin."
)


def _process_job_sync(
    input_path: str, output_dir: Path, budget: VisionBudget | None = None
) -> tuple[str, str]:
    """Run the full pipeline synchronously; return (docx_path, pptx_path).

    ``budget`` caps the Gemini requests of the job (retries included) and is
    shared with the caller, which reads ``budget.used`` afterwards. Raises
    ``VisionBudgetExceededError`` when the ceiling is hit.

    This is the SAME pipeline as ``scripts/assemble_document.py`` —
    extract → classify → assemble, with vision called inside the
    assemblers for DIAGRAM elements. Both assemblers share one cached
    provider, so each diagram image costs ONE vision call per job.
    """
    source = Path(input_path)
    classified = classify_elements(get_extractor(source).extract(source))

    output_dir.mkdir(parents=True, exist_ok=True)

    if budget is None:
        budget = VisionBudget(limit=get_settings().max_vision_calls_per_job)
    vision = CachedVisionProvider(factory=lambda: get_vision_provider(budget))
    document = assemble_docx(classified, vision_provider=vision)
    docx_path = output_dir / "output.docx"
    document.save(str(docx_path))

    prs = assemble_pptx(classified, vision_provider=vision)
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

        # Requests already spent by earlier attempts count against the ceiling,
        # so a retry cannot start the allowance over.
        budget = VisionBudget(
            limit=get_settings().max_vision_calls_per_job, used=job.vision_calls
        )

        try:
            docx_path, pptx_path = await asyncio.to_thread(
                _process_job_sync, job.input_path, output_dir, budget
            )
            store.update_job(
                job.id,
                status=JobStatus.COMPLETED,
                output_docx_path=docx_path,
                output_pptx_path=pptx_path,
                error_message=None,
                vision_calls=budget.used,
            )
            logger.info("Job %s completed", job.id)
        except VisionBudgetExceededError:
            # Terminal: re-running would only spend more, so no retry.
            store.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_message=_BUDGET_EXCEEDED_MESSAGE,
                vision_calls=budget.used,
            )
            logger.error(
                "Job %s stopped: Gemini request limit (%d) reached",
                job.id,
                budget.limit,
            )
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
                    vision_calls=budget.used,
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
                    vision_calls=budget.used,
                )
                logger.error("Job %s failed permanently: %s", job.id, exc)


async def start_worker(store: JobStore) -> asyncio.Task[None]:
    """Launch the worker loop as a background asyncio task."""
    task = asyncio.create_task(_worker_loop(store))
    logger.info("Background worker started")
    return task
