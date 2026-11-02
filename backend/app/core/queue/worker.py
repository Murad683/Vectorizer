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
import time
import traceback
from pathlib import Path

from app.config import get_settings
from app.core import vision as vision_pkg
from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.ingestion import count_pdf_pages, get_extractor
from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore
from app.core.vision import VisionBudget, VisionBudgetExceededError
from app.core.vision.cache import CachedVisionProvider

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0
_JOBS_OUTPUT_DIR = Path("data/jobs")


_BUDGET_EXCEEDED_MESSAGE = (
    "Bu iş üçün icazə verilən süni intellekt sorğularının sayı aşıldı. "
    "Faylı bölüb daha kiçik hissələrlə yenidən yükləyin."
)


def _count_input_pages(path: Path) -> int | None:
    """Pages of the input for the usage log: PDF page count, 1 per image."""
    try:
        ext = path.suffix.lower()
        if ext == ".pdf":
            return count_pdf_pages(path.read_bytes())
        if ext in {".png", ".jpg", ".jpeg"}:
            return 1
    except Exception:  # noqa: BLE001 - the log is optional
        logger.debug("Could not count the pages of %s", path.name, exc_info=True)
    return None


def _vision_limit(input_path: str) -> int:
    """Gemini request ceiling for one job.

    A PDF costs one request per scanned page (and a retry now and then), so
    its ceiling grows with the page count; everything else keeps the flat
    ``MAX_VISION_CALLS_PER_JOB``.
    """
    settings = get_settings()
    limit = settings.max_vision_calls_per_job
    path = Path(input_path)
    if path.suffix.lower() == ".pdf":
        pages = _count_input_pages(path)
        if pages:
            limit = max(
                limit,
                pages * settings.vision_calls_per_page + settings.vision_calls_extra,
            )
    return limit


def _record_usage(
    store: JobStore,
    job: Job,
    budget: VisionBudget,
    calls_before: int,
    started: float,
    outcome: str,
) -> None:
    """Write one usage row for this attempt; never lets a failure out."""
    try:
        settings = get_settings()
        cost = (
            budget.input_tokens * settings.gemini_price_in_per_m
            + (budget.output_tokens + budget.thought_tokens)
            * settings.gemini_price_out_per_m
        ) / 1_000_000
        path = Path(job.input_path)
        store.record_usage(
            job_id=job.id,
            owner_user_id=job.owner_user_id,
            file_ext=path.suffix.lower().lstrip("."),
            pages=_count_input_pages(path),
            vision_calls=budget.used - calls_before,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            thought_tokens=budget.thought_tokens,
            est_cost_usd=cost,
            duration_s=time.monotonic() - started,
            outcome=outcome,
        )
    except Exception:  # noqa: BLE001 - the usage log must never fail a job
        logger.warning("Could not write the usage row of job %s", job.id, exc_info=True)


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
        budget = VisionBudget(limit=_vision_limit(input_path))
    vision = CachedVisionProvider(
        factory=lambda: vision_pkg.get_vision_provider(budget)
    )
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
            limit=_vision_limit(job.input_path), used=job.vision_calls
        )

        calls_before = budget.used
        started = time.monotonic()
        claimed = job

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
            _record_usage(store, claimed, budget, calls_before, started, "completed")
            logger.info("Job %s completed", job.id)
        except VisionBudgetExceededError:
            # Terminal: re-running would only spend more, so no retry.
            store.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_message=_BUDGET_EXCEEDED_MESSAGE,
                vision_calls=budget.used,
            )
            _record_usage(store, claimed, budget, calls_before, started, "budget")
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
                _record_usage(store, claimed, budget, calls_before, started, "retry")
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
                _record_usage(store, claimed, budget, calls_before, started, "failed")
                logger.error("Job %s failed permanently: %s", job.id, exc)


async def start_worker(store: JobStore) -> asyncio.Task[None]:
    """Launch the worker loop as a background asyncio task."""
    task = asyncio.create_task(_worker_loop(store))
    logger.info("Background worker started")
    return task
