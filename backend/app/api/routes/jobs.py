"""Job management API: upload, poll status, download results."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import get_settings
from app.core.auth.dependencies import get_current_user
from app.core.auth.models import Role, User
from app.core.ingestion import count_pdf_pages
from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore
from app.models.errors import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

_UPLOAD_DIR = Path("data/uploads")
_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}


def _error(
    status_code: int, error_code: str, message: str, request: Request
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error_code=error_code, message=message, request_id=request_id
        ).model_dump(),
    )


def _get_store(request: Request) -> JobStore:
    return request.app.state.job_store  # type: ignore[no-any-return]


def _check_ownership(job: Job, user: User, request: Request) -> JSONResponse | None:
    """Return a 403 response if user doesn't own the job and isn't admin."""
    if user.role == Role.ADMIN:
        return None
    if job.owner_user_id != user.id:
        return _error(403, "forbidden", "You do not have access to this job.", request)
    return None


@router.post("", response_model=None)
async def create_job(
    file: UploadFile,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """Accept a file upload, create a PENDING job, return immediately."""
    store = _get_store(request)
    settings = get_settings()

    if not file.filename:
        return _error(
            400, "missing_filename", "Uploaded file has no filename.", request
        )

    ext = Path(file.filename).suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        return _error(
            400,
            "unsupported_format",
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}",
            request,
        )

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    contents = await file.read()
    if len(contents) > max_bytes:
        return _error(
            413,
            "file_too_large",
            f"File exceeds the {settings.max_upload_size_mb} MB upload limit.",
            request,
        )

    if ext == ".pdf":
        pages = await asyncio.to_thread(count_pdf_pages, contents)
        if pages is not None and pages > settings.max_pages_per_file:
            return _error(
                400,
                "too_many_pages",
                f"PDF faylında {pages} səhifə var; icazə verilən maksimum "
                f"{settings.max_pages_per_file} səhifədir. Faylı bölüb "
                "yenidən yükləyin.",
                request,
            )

    # No await between these checks and create_job below, so two uploads of
    # the same user cannot both slip past the limits.
    if store.count_active_jobs(current_user.id) >= settings.max_active_jobs_per_user:
        return _error(
            429,
            "too_many_active_jobs",
            f"Eyni anda ən çox {settings.max_active_jobs_per_user} iş "
            "gözləyə və ya işlənə bilər. Əvvəlki işlərin bitməsini gözləyin.",
            request,
        )
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    if (
        store.count_submissions_since(current_user.id, day_ago)
        >= settings.max_jobs_per_user_per_day
    ):
        return _error(
            429,
            "daily_job_limit",
            f"Son 24 saat ərzində ən çox {settings.max_jobs_per_user_per_day} "
            "iş göndərə bilərsiniz. Limit dolub, sonra yenidən cəhd edin.",
            request,
        )

    job_id = uuid4()
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    input_path = _UPLOAD_DIR / f"{job_id}{ext}"
    input_path.write_bytes(contents)

    job = store.create_job(
        job_id=job_id,
        input_filename=file.filename,
        input_path=str(input_path),
        owner_user_id=current_user.id,
    )
    logger.info(
        "Created job %s for %s (user %s)", job.id, file.filename, current_user.email
    )
    return JSONResponse(
        status_code=201,
        content={"job_id": str(job.id), "status": job.status.value},
    )


_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


@router.get("", response_model=None)
async def list_jobs(
    request: Request,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """Return a page of the caller's jobs (admins see all jobs).

    ``limit``/``offset`` paginate, ``search`` case-insensitively matches
    against the filename. Response shape: ``{items: [...], total: N}``,
    ``total`` reflecting the filtered (not paginated) count.
    """
    store = _get_store(request)
    limit = max(1, min(limit, _MAX_PAGE_SIZE))
    offset = max(0, offset)
    owner_filter = None if current_user.role == Role.ADMIN else current_user.id

    items = store.list_jobs(
        owner_user_id=owner_filter, search=search, limit=limit, offset=offset
    )
    total = store.count_jobs(owner_user_id=owner_filter, search=search)
    return JSONResponse(
        content={"items": [_job_response(j) for j in items], "total": total}
    )


@router.get("/{job_id}", response_model=None)
async def get_job_status(
    job_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """Return the current status of a job."""
    store = _get_store(request)
    job = store.get_job(job_id)
    if job is None:
        return _error(404, "job_not_found", f"No job with id {job_id}.", request)

    denied = _check_ownership(job, current_user, request)
    if denied is not None:
        return denied

    return JSONResponse(content=_job_response(job))


def _job_response(job: Job) -> dict[str, Any]:
    """Build the JSON response body for a job."""
    body: dict[str, Any] = {
        "job_id": str(job.id),
        "status": job.status.value,
        "input_filename": job.input_filename,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }
    if job.status == JobStatus.COMPLETED:
        formats = []
        if job.output_docx_path:
            formats.append("docx")
        if job.output_pptx_path:
            formats.append("pptx")
        body["available_formats"] = formats
    if job.status == JobStatus.FAILED:
        body["error_message"] = job.error_message
    return body


@router.get("/{job_id}/download", response_model=None)
async def download_job_output(
    job_id: UUID,
    format: str,  # noqa: A002
    request: Request,
    current_user: User = Depends(get_current_user),
) -> FileResponse | JSONResponse:
    """Stream a completed job's output file.

    Returns 404 if the job doesn't exist, 403 if the user doesn't own
    it (and isn't admin), 409 if the job hasn't completed yet, and 400
    if the requested format is invalid.
    """
    store = _get_store(request)
    job = store.get_job(job_id)
    if job is None:
        return _error(404, "job_not_found", f"No job with id {job_id}.", request)

    denied = _check_ownership(job, current_user, request)
    if denied is not None:
        return denied

    if job.status != JobStatus.COMPLETED:
        return _error(
            409,
            "job_not_completed",
            f"Job is {job.status.value}, not completed.",
            request,
        )

    fmt = format.lower()
    if fmt == "docx":
        path = job.output_docx_path
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    elif fmt == "pptx":
        path = job.output_pptx_path
        media_type = (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
    else:
        return _error(
            400, "invalid_format", "format must be 'docx' or 'pptx'.", request
        )

    if not path or not Path(path).exists():
        return _error(
            404, "output_not_found", f"Output file ({fmt}) not found on disk.", request
        )

    stem = Path(job.input_filename).stem
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=f"{stem}.{fmt}",
    )


@router.delete("/{job_id}", response_model=None, status_code=204)
async def delete_job(
    job_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response | JSONResponse:
    """Delete a job and its files from disk.

    Returns 404 if the job doesn't exist, 403 if the user doesn't own
    it (and isn't admin), 204 on success.
    """
    store = _get_store(request)
    job = store.get_job(job_id)
    if job is None:
        return _error(404, "job_not_found", f"No job with id {job_id}.", request)

    denied = _check_ownership(job, current_user, request)
    if denied is not None:
        return denied

    for path_str in (job.input_path, job.output_docx_path, job.output_pptx_path):
        if not path_str:
            continue
        path = Path(path_str)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                logger.warning("Failed to delete file %s for job %s", path, job.id)

    store.delete_job(job.id)
    logger.info(
        "Deleted job %s (%s) by %s", job.id, job.input_filename, current_user.email
    )
    return Response(status_code=204)
