"""Delete uploaded input files that are no longer needed.

Run by a daily timer on the server::

    python -m app.cleanup_inputs [--dry-run] [--hours N] [--max N]

Only INPUT files are ever removed. Deleted:

- the input file of a COMPLETED or FAILED job, ``INPUT_RETENTION_HOURS`` after
  the job reached that state (``jobs.updated_at``);
- an upload with no job row at all (a crashed upload) that old;
- an empty ``data/jobs/<uuid>`` directory whose job is not waiting or running.

Never touched: files of PENDING / PROCESSING jobs (they may still be read, and a
failed attempt puts a job back to PENDING), result files (DOCX/PPTX), directories
that still hold files, and the job rows themselves. The only calls that remove
anything are ``Path.unlink`` and ``Path.rmdir``; there is no recursive delete.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.core.queue.models import JobStatus
from app.core.queue.store import JobStore
from app.logging_config import configure_logging

logger = logging.getLogger("app.cleanup_inputs")

UPLOADS_DIR = Path("data/uploads")
JOBS_DIR = Path("data/jobs")

_ACTIVE = (JobStatus.PENDING, JobStatus.PROCESSING)


@dataclass(frozen=True)
class Candidate:
    """One thing the cleanup would delete."""

    path: Path
    kind: str  # "input" | "orphan_input" | "empty_dir"
    job_id: UUID | None
    detail: str
    size: int
    age_hours: float


@dataclass
class CleanupResult:
    """What a run found and did."""

    dry_run: bool
    candidates: list[Candidate] = field(default_factory=list)
    deleted: list[Candidate] = field(default_factory=list)
    skipped_over_limit: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def _as_uuid(name: str) -> UUID | None:
    try:
        return UUID(name)
    except ValueError:
        return None


def _age_hours(path: Path, now: datetime) -> float:
    modified = datetime.fromtimestamp(path.lstat().st_mtime, tz=timezone.utc)
    return (now - modified).total_seconds() / 3600


def _is_plain_file_in(path: Path, directory: Path) -> bool:
    """True for a regular file (not a symlink) sitting directly in ``directory``."""
    if path.is_symlink() or not path.is_file():
        return False
    return path.resolve().parent == directory.resolve()


def find_candidates(
    store: JobStore,
    *,
    uploads_dir: Path,
    jobs_dir: Path,
    retention_hours: float,
    now: datetime,
) -> tuple[list[Candidate], list[str]]:
    """Return (things to delete, report-only notes). Deletes nothing."""
    cutoff = now - timedelta(hours=retention_hours)
    found: dict[Path, Candidate] = {}
    notes: list[str] = []

    for job in store.list_finished_jobs_before(cutoff):
        path = Path(job.input_path)
        if not path.exists() and not path.is_symlink():
            continue  # already gone (job deleted by the user, or cleaned earlier)
        if not _is_plain_file_in(path, uploads_dir):
            notes.append(
                f"job {job.id}: input path {path} is not a plain file in "
                "uploads; skipped"
            )
            continue
        found[path] = Candidate(
            path=path,
            kind="input",
            job_id=job.id,
            detail=f"{job.status.value} job, file {job.input_filename!r}",
            size=path.stat().st_size,
            age_hours=(now - job.updated_at).total_seconds() / 3600,
        )

    if uploads_dir.is_dir():
        for path in sorted(uploads_dir.iterdir()):
            job_id = _as_uuid(path.stem)
            if (
                path in found
                or job_id is None
                or not _is_plain_file_in(path, uploads_dir)
            ):
                continue
            if store.get_job(job_id) is not None:
                continue  # belongs to a job: handled above, or still needed
            age = _age_hours(path, now)
            if age >= retention_hours:
                found[path] = Candidate(
                    path=path,
                    kind="orphan_input",
                    job_id=job_id,
                    detail="upload without a job row",
                    size=path.stat().st_size,
                    age_hours=age,
                )

    if jobs_dir.is_dir():
        for path in sorted(jobs_dir.iterdir()):
            job_id = _as_uuid(path.name)
            if job_id is None or path.is_symlink() or not path.is_dir():
                continue
            if any(path.iterdir()):
                if store.get_job(job_id) is None:
                    notes.append(
                        f"{path}: has files but no job row; left alone, please check"
                    )
                continue
            owner = store.get_job(job_id)
            if owner is not None and owner.status in _ACTIVE:
                continue
            age = _age_hours(path, now)
            if age >= retention_hours:
                found[path] = Candidate(
                    path=path,
                    kind="empty_dir",
                    job_id=job_id,
                    detail="empty result directory",
                    size=0,
                    age_hours=age,
                )

    return sorted(found.values(), key=lambda c: (c.kind, str(c.path))), notes


def run_cleanup(
    store: JobStore,
    *,
    retention_hours: float,
    max_deletions: int,
    dry_run: bool,
    uploads_dir: Path = UPLOADS_DIR,
    jobs_dir: Path = JOBS_DIR,
    now: datetime | None = None,
) -> CleanupResult:
    """Find and (unless ``dry_run``) delete stale input files."""
    now = now or datetime.now(timezone.utc)
    result = CleanupResult(dry_run=dry_run)
    result.candidates, result.notes = find_candidates(
        store,
        uploads_dir=uploads_dir,
        jobs_dir=jobs_dir,
        retention_hours=retention_hours,
        now=now,
    )
    for note in result.notes:
        logger.warning("note: %s", note)

    for candidate in result.candidates:
        line = (
            f"{candidate.kind} {candidate.path} job={candidate.job_id} "
            f"size={candidate.size}B age={candidate.age_hours:.1f}h "
            f"({candidate.detail})"
        )
        if len(result.deleted) >= max_deletions:
            result.skipped_over_limit += 1
            continue
        if dry_run:
            logger.info("WOULD DELETE %s", line)
            result.deleted.append(candidate)
            continue
        try:
            # Re-check right before deleting: a job row may have appeared.
            if candidate.kind != "input" and candidate.job_id is not None:
                job = store.get_job(candidate.job_id)
                if candidate.kind == "orphan_input" and job is not None:
                    continue
                if job is not None and job.status in _ACTIVE:
                    continue
            if candidate.kind == "empty_dir":
                candidate.path.rmdir()
            else:
                candidate.path.unlink()
        except OSError as exc:
            result.errors += 1
            logger.error("FAILED to delete %s: %s", line, exc)
            continue
        logger.info("DELETED %s", line)
        result.deleted.append(candidate)

    logger.info(
        "input cleanup %s: %d candidate(s), %d %s, %d left for the next run "
        "(limit %d), %d error(s), keep=%sh",
        "dry-run" if dry_run else "run",
        len(result.candidates),
        len(result.deleted),
        "would be deleted" if dry_run else "deleted",
        result.skipped_over_limit,
        max_deletions,
        result.errors,
        retention_hours,
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="only list, delete nothing"
    )
    parser.add_argument("--hours", type=float, help="override INPUT_RETENTION_HOURS")
    parser.add_argument(
        "--max", type=int, dest="max_deletions", help="override the per-run limit"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    dry_run = args.dry_run or settings.input_cleanup_dry_run
    hours = settings.input_retention_hours if args.hours is None else args.hours
    limit = (
        settings.input_cleanup_max_per_run
        if args.max_deletions is None
        else args.max_deletions
    )

    if hours < 1 and not dry_run:
        parser.error("--hours below 1 is only allowed together with --dry-run")
    if limit < 1:
        parser.error("--max must be at least 1")

    store = JobStore(dsn=settings.database_url)
    try:
        result = run_cleanup(
            store, retention_hours=hours, max_deletions=limit, dry_run=dry_run
        )
    finally:
        store.close()
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
