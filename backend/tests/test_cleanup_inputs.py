"""Tests for the daily input-file cleanup (app.cleanup_inputs)."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.cleanup_inputs import CleanupResult, main, run_cleanup
from app.config import get_settings
from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    uploads = tmp_path / "uploads"
    jobs = tmp_path / "jobs"
    uploads.mkdir()
    jobs.mkdir()
    return uploads, jobs


def _make_job(
    store: JobStore,
    uploads: Path,
    jobs: Path,
    status: JobStatus,
    *,
    age_hours: float = 48,
    with_output: bool = True,
) -> Job:
    """A job with an input file (and result files), last updated ``age_hours`` ago."""
    job_id = uuid4()
    input_path = uploads / f"{job_id}.png"
    input_path.write_bytes(b"input")
    job = store.create_job(
        job_id=job_id, input_filename="a.png", input_path=str(input_path)
    )
    if with_output:
        out = jobs / str(job_id)
        out.mkdir()
        (out / "output.docx").write_bytes(b"docx")
        (out / "output.pptx").write_bytes(b"pptx")
    store.update_job(job_id, status=status)
    updated = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    store._conn.execute(
        "UPDATE jobs SET updated_at = %s WHERE id = %s", (updated, job_id)
    )
    return job


def _run(store: JobStore, dirs: tuple[Path, Path], **kwargs: object) -> CleanupResult:
    params: dict[str, object] = {
        "retention_hours": 24,
        "max_deletions": 100,
        "dry_run": False,
    }
    params.update(kwargs)
    return run_cleanup(
        store, uploads_dir=dirs[0], jobs_dir=dirs[1], **params  # type: ignore[arg-type]
    )


class TestFinishedJobs:
    @pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED])
    def test_old_finished_job_loses_only_its_input(
        self, job_store: JobStore, dirs: tuple[Path, Path], status: JobStatus
    ) -> None:
        job = _make_job(job_store, *dirs, status)
        _run(job_store, dirs)
        assert not Path(job.input_path).exists()
        out = dirs[1] / str(job.id)
        assert (out / "output.docx").read_bytes() == b"docx"
        assert (out / "output.pptx").read_bytes() == b"pptx"
        assert job_store.get_job(job.id) is not None

    def test_recent_finished_job_is_kept(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        job = _make_job(job_store, *dirs, JobStatus.COMPLETED, age_hours=2)
        _run(job_store, dirs)
        assert Path(job.input_path).exists()

    @pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.PROCESSING])
    def test_waiting_or_running_job_is_never_touched(
        self, job_store: JobStore, dirs: tuple[Path, Path], status: JobStatus
    ) -> None:
        job = _make_job(job_store, *dirs, status, age_hours=500, with_output=False)
        # even an old, empty result directory of such a job stays
        (dirs[1] / str(job.id)).mkdir()
        os.utime(dirs[1] / str(job.id), (0, 0))
        _run(job_store, dirs)
        assert Path(job.input_path).exists()
        assert (dirs[1] / str(job.id)).is_dir()

    def test_retry_in_progress_keeps_input(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        """A failed attempt puts the job back to PENDING: its input must stay."""
        job = _make_job(job_store, *dirs, JobStatus.PROCESSING, age_hours=100)
        job_store.update_job(job.id, status=JobStatus.PENDING, retry_count=1)
        store_age = datetime.now(timezone.utc) - timedelta(hours=100)
        job_store._conn.execute(
            "UPDATE jobs SET updated_at = %s WHERE id = %s", (store_age, job.id)
        )
        _run(job_store, dirs)
        assert Path(job.input_path).exists()

    def test_already_deleted_input_is_not_an_error(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        job = _make_job(job_store, *dirs, JobStatus.COMPLETED)
        Path(job.input_path).unlink()
        result = _run(job_store, dirs)
        assert result.errors == 0
        assert result.candidates == []

    def test_second_run_finds_nothing(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        _make_job(job_store, *dirs, JobStatus.COMPLETED)
        assert len(_run(job_store, dirs).deleted) == 1
        assert _run(job_store, dirs).candidates == []


class TestDryRunAndLimit:
    def test_dry_run_deletes_nothing_but_reports(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        job = _make_job(job_store, *dirs, JobStatus.COMPLETED)
        result = _run(job_store, dirs, dry_run=True)
        assert len(result.deleted) == 1
        assert Path(job.input_path).exists()

    def test_per_run_limit(self, job_store: JobStore, dirs: tuple[Path, Path]) -> None:
        jobs = [_make_job(job_store, *dirs, JobStatus.COMPLETED) for _ in range(5)]
        result = _run(job_store, dirs, max_deletions=2)
        assert len(result.deleted) == 2
        assert result.skipped_over_limit == 3
        assert sum(Path(j.input_path).exists() for j in jobs) == 3
        _run(job_store, dirs, max_deletions=10)
        assert not any(Path(j.input_path).exists() for j in jobs)


class TestLeftovers:
    def test_orphan_upload_deleted_only_when_old(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        old = dirs[0] / f"{uuid4()}.pdf"
        new = dirs[0] / f"{uuid4()}.pdf"
        other = dirs[0] / "notes.txt"
        for p in (old, new, other):
            p.write_bytes(b"x")
        os.utime(old, (0, 0))
        os.utime(other, (0, 0))
        _run(job_store, dirs)
        assert not old.exists()
        assert new.exists()
        assert other.exists()  # name is not a job id

    def test_upload_of_a_waiting_job_is_not_an_orphan(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        job = _make_job(job_store, *dirs, JobStatus.PENDING, with_output=False)
        os.utime(job.input_path, (0, 0))
        _run(job_store, dirs)
        assert Path(job.input_path).exists()

    def test_empty_result_dir_removed_but_filled_dir_kept(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        empty = dirs[1] / str(uuid4())
        filled = dirs[1] / str(uuid4())
        young = dirs[1] / str(uuid4())
        for d in (empty, filled, young):
            d.mkdir()
        (filled / "output.docx").write_bytes(b"docx")
        for d in (empty, filled):
            os.utime(d, (0, 0))
        result = _run(job_store, dirs)
        assert not empty.exists()
        assert (filled / "output.docx").exists()  # no job row, still untouched
        assert young.exists()
        assert any("has files but no job row" in n for n in result.notes)


class TestSafety:
    def test_symlink_and_outside_paths_are_not_followed(
        self, job_store: JobStore, dirs: tuple[Path, Path], tmp_path: Path
    ) -> None:
        outside = tmp_path / "precious.png"
        outside.write_bytes(b"keep")
        # a finished job whose stored path points outside the uploads dir
        job_id = uuid4()
        job_store.create_job(
            job_id=job_id, input_filename="x.png", input_path=str(outside)
        )
        job_store.update_job(job_id, status=JobStatus.COMPLETED)
        job_store._conn.execute(
            "UPDATE jobs SET updated_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(hours=99), job_id),
        )
        # a symlink inside uploads
        link = dirs[0] / f"{uuid4()}.png"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            link = None  # type: ignore[assignment]
        os.utime(outside, (0, 0))

        result = _run(job_store, dirs)
        assert outside.read_bytes() == b"keep"
        assert result.candidates == []
        if link is not None:
            assert link.is_symlink()

    def test_result_files_never_deleted_even_for_old_jobs(
        self, job_store: JobStore, dirs: tuple[Path, Path]
    ) -> None:
        job = _make_job(job_store, *dirs, JobStatus.COMPLETED, age_hours=24 * 400)
        _run(job_store, dirs, retention_hours=1)
        assert (dirs[1] / str(job.id) / "output.docx").exists()
        assert (dirs[1] / str(job.id) / "output.pptx").exists()


class TestCommandLine:
    def test_dry_run_flag(
        self,
        job_store: JobStore,
        dirs: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job = _make_job(job_store, *dirs, JobStatus.COMPLETED)
        monkeypatch.setattr("app.cleanup_inputs.UPLOADS_DIR", dirs[0])
        monkeypatch.setattr("app.cleanup_inputs.JOBS_DIR", dirs[1])
        monkeypatch.setattr("app.cleanup_inputs.JobStore", lambda dsn: job_store)
        assert main(["--dry-run"]) == 0
        assert Path(job.input_path).exists()

    def test_real_run_refuses_zero_hours(self) -> None:
        with pytest.raises(SystemExit):
            main(["--hours", "0"])

    def test_defaults(self) -> None:
        settings = get_settings()
        assert settings.input_retention_hours == 24
        assert settings.input_cleanup_max_per_run == 100
        assert settings.result_retention_days == 0
