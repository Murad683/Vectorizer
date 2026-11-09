"""What a finished job leaves for us to learn from: saved answers, gap check, kinds.

Offline: the pipeline fills a report, ``_after_job`` saves the model's answers
and counts what the conversion lost, the cleanup removes old answers, the
reports print. The ``...Database`` classes need PostgreSQL and run in CI.
"""

import asyncio
import contextlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.cleanup_inputs import find_candidates, run_cleanup
from app.config import get_settings
from app.core import vision as vision_pkg
from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore
from app.core.queue.worker import (
    JobReport,
    _after_job,
    _job_report,
    _process_job_sync,
    _record_usage,
    _worker_loop,
)
from app.core.vision import VisionBudget
from app.core.vision.base import VisionProvider
from app.core.vision.gemini_provider import GeminiVisionProvider
from app.eval.replay import ReplayProvider, load_results, result_key, run_pipeline
from app.eval.synth import SynthPage, options_with_pictures, text_lines
from app.models.document import ContentType, VisionResult
from app.models.page import PageAnalysis
from app.quality_report import build_report as quality_report
from app.usage_report import build_report as usage_report


class _Model(VisionProvider):
    """A model that always gives the same page answer."""

    def __init__(self, answer: PageAnalysis) -> None:
        self.answer = answer

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        return VisionResult(content_type=ContentType.PAGE, page=self.answer)


class _Store:
    def __init__(self, fail: bool = False) -> None:
        self.quality: list[dict[str, Any]] = []
        self._fail = fail

    def record_quality(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("database is down")
        self.quality.append(kwargs)


def _job(tmp_path: Path) -> Job:
    return Job(
        id=uuid4(),
        owner_user_id=uuid4(),
        input_filename="x.png",
        input_path=str(tmp_path / "x.png"),
    )


def _run_pipeline(
    page: SynthPage,
    answer: PageAnalysis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> JobReport:
    """Run the worker's pipeline function for a page with a fixed model answer."""
    source = tmp_path / "source.png"
    source.write_bytes(page.png)
    monkeypatch.setattr(
        vision_pkg, "get_vision_provider", lambda budget: _Model(answer)
    )
    report = JobReport()
    token = _job_report.set(report)
    try:
        _process_job_sync(str(source), tmp_path / "out", VisionBudget(limit=25))
    finally:
        _job_report.reset(token)
    return report


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestKindsOfRequests:
    def test_the_budget_counts_requests_by_kind(self) -> None:
        budget = VisionBudget(limit=9)
        budget.add_usage(10, 5, None, kind="page")
        budget.add_usage(10, 5, None, kind="page_repeat")
        budget.add_usage(10, 5, None, kind="page")
        budget.add_usage(10, 5, None)  # no kind: only the tokens are added
        assert budget.by_kind == {"page": 2, "page_repeat": 1}
        assert budget.input_tokens == 40

    def test_the_provider_passes_the_kind_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, GEMINI_API_KEY="test-key-not-real")
        monkeypatch.setattr("app.core.vision.gemini_provider.genai.Client", MagicMock())
        budget = VisionBudget(limit=5)
        provider = GeminiVisionProvider(budget)
        response = MagicMock()
        response.usage_metadata.prompt_token_count = 100
        response.usage_metadata.candidates_token_count = 20
        response.usage_metadata.thoughts_token_count = None
        provider._note_usage(response, "flowchart_rescue")
        assert budget.by_kind == {"flowchart_rescue": 1}

    def test_the_usage_row_carries_the_kinds(self, tmp_path: Path) -> None:
        rows: list[dict[str, Any]] = []

        class Store:
            def record_usage(self, **kwargs: Any) -> None:
                rows.append(kwargs)

        budget = VisionBudget(limit=9)
        budget.consume()
        budget.add_usage(1, 1, None, kind="page")
        _record_usage(Store(), _job(tmp_path), budget, 0, 0.0, "completed")  # type: ignore[arg-type]
        assert rows[0]["calls_by_kind"] == {"page": 1}
        # nothing counted: no kinds, not an empty dict
        rows.clear()
        _record_usage(Store(), _job(tmp_path), VisionBudget(limit=1), 0, 0.0, "x")  # type: ignore[arg-type]
        assert rows[0]["calls_by_kind"] is None


class TestPipelineFillsTheReport:
    def test_answers_images_and_word_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = text_lines()
        report = _run_pipeline(page, page.good, tmp_path, monkeypatch)
        assert list(report.answers) == [result_key(page.png)]
        assert report.images == [page.png]
        assert report.docx_path is not None and report.docx_path.exists()

    def test_nothing_is_kept_when_the_function_is_called_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = text_lines()
        source = tmp_path / "source.png"
        source.write_bytes(page.png)
        monkeypatch.setattr(
            vision_pkg, "get_vision_provider", lambda budget: _Model(page.good)
        )
        docx_path, _ = _process_job_sync(
            str(source), tmp_path / "out", VisionBudget(limit=25)
        )
        assert Path(docx_path).exists()
        assert _job_report.get() is None


class TestAfterJob:
    def test_a_good_answer_is_saved_and_counts_no_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = text_lines()
        report = _run_pipeline(page, page.good, tmp_path, monkeypatch)
        monkeypatch.setattr("app.core.queue.worker.ANSWERS_DIR", tmp_path / "answers")
        store, job = _Store(), _job(tmp_path)
        _after_job(store, job, report)  # type: ignore[arg-type]

        saved = tmp_path / "answers" / f"{job.id}.json"
        assert saved.exists()
        loaded = load_results(saved, [page.png])
        assert loaded[result_key(page.png)].page is not None
        assert store.quality == [
            {
                "job_id": job.id,
                "owner_user_id": job.owner_user_id,
                "pages_checked": 1,
                "errors": 0,
                "reviews": 0,
                "kinds": {},
            }
        ]

    def test_a_broken_answer_is_counted_by_kind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = options_with_pictures()
        report = _run_pipeline(page, page.broken, tmp_path, monkeypatch)
        monkeypatch.setattr("app.core.queue.worker.ANSWERS_DIR", tmp_path / "answers")
        store = _Store()
        _after_job(store, _job(tmp_path), report)  # type: ignore[arg-type]
        row = store.quality[0]
        assert row["errors"] >= 5
        assert row["kinds"]["empty_option"] == 5
        assert "uncovered_ink" in row["kinds"]

    def test_the_two_switches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = text_lines()
        report = _run_pipeline(page, page.good, tmp_path, monkeypatch)
        monkeypatch.setattr("app.core.queue.worker.ANSWERS_DIR", tmp_path / "answers")

        _env(monkeypatch, SAVE_PAGE_ANSWERS="false")
        store, job = _Store(), _job(tmp_path)
        _after_job(store, job, report)  # type: ignore[arg-type]
        assert not (tmp_path / "answers").exists() and len(store.quality) == 1

        _env(monkeypatch, SAVE_PAGE_ANSWERS="true", AUTO_GAP_CHECK="false")
        store = _Store()
        _after_job(store, job, report)  # type: ignore[arg-type]
        assert (tmp_path / "answers" / f"{job.id}.json").exists()
        assert store.quality == []

    def test_a_broken_database_never_reaches_the_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = text_lines()
        report = _run_pipeline(page, page.good, tmp_path, monkeypatch)
        monkeypatch.setattr("app.core.queue.worker.ANSWERS_DIR", tmp_path / "answers")
        _after_job(_Store(fail=True), _job(tmp_path), report)  # type: ignore[arg-type]

    def test_nothing_was_asked_of_the_model(self, tmp_path: Path) -> None:
        store = _Store()
        _after_job(store, _job(tmp_path), JobReport())  # type: ignore[arg-type]
        assert store.quality == []


class _OldJobsStore:
    """Just enough of a store for the cleanup: no finished jobs, one optional job."""

    def __init__(self, job: Job | None = None) -> None:
        self._job = job

    def list_finished_jobs_before(self, _cutoff: datetime) -> list[Job]:
        return []

    def get_job(self, _job_id: UUID) -> Job | None:
        return self._job


def _aged(path: Path, hours: float) -> Path:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))
    return path


class TestCleanupOfAnswers:
    def _answers(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        folder = tmp_path / "answers"
        folder.mkdir()
        old, new = folder / f"{uuid4()}.json", folder / f"{uuid4()}.json"
        old.write_text("{}")
        new.write_text("{}")
        _aged(old, 40)
        _aged(new, 2)
        (folder / "notes.json").write_text("{}")
        _aged(folder / "notes.json", 99)
        (folder / f"{uuid4()}.txt").write_text("x")
        return folder, old, new

    def test_only_old_answers_are_candidates(self, tmp_path: Path) -> None:
        folder, old, new = self._answers(tmp_path)
        candidates, _ = find_candidates(
            _OldJobsStore(),  # type: ignore[arg-type]
            uploads_dir=tmp_path / "uploads",
            jobs_dir=tmp_path / "jobs",
            retention_hours=24,
            now=datetime.now(timezone.utc),
            answers_dir=folder,
        )
        assert [(c.kind, c.path) for c in candidates] == [("answers", old)]

    def test_old_answers_are_deleted_and_new_ones_stay(self, tmp_path: Path) -> None:
        folder, old, new = self._answers(tmp_path)
        result = run_cleanup(
            _OldJobsStore(),  # type: ignore[arg-type]
            retention_hours=24,
            max_deletions=10,
            dry_run=False,
            uploads_dir=tmp_path / "uploads",
            jobs_dir=tmp_path / "jobs",
            answers_dir=folder,
        )
        assert not old.exists() and new.exists()
        assert (folder / "notes.json").exists()  # not a job's file name
        assert len(result.deleted) == 1

    def test_the_answers_of_a_running_job_stay(self, tmp_path: Path) -> None:
        folder, old, _ = self._answers(tmp_path)
        job = Job(
            id=UUID(old.stem),
            input_filename="x",
            input_path="x",
            status=JobStatus.PROCESSING,
        )
        run_cleanup(
            _OldJobsStore(job),  # type: ignore[arg-type]
            retention_hours=24,
            max_deletions=10,
            dry_run=False,
            uploads_dir=tmp_path / "uploads",
            jobs_dir=tmp_path / "jobs",
            answers_dir=folder,
        )
        assert old.exists()

    def test_without_an_answers_folder_nothing_changes(self, tmp_path: Path) -> None:
        candidates, _ = find_candidates(
            _OldJobsStore(),  # type: ignore[arg-type]
            uploads_dir=tmp_path / "uploads",
            jobs_dir=tmp_path / "jobs",
            retention_hours=24,
            now=datetime.now(timezone.utc),
        )
        assert candidates == []


class TestReports:
    def _rows(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        job = uuid4()
        return [
            {
                "created_at": now,
                "job_id": job,
                "pages_checked": 3,
                "errors": 5,
                "reviews": 1,
                "kinds": {"empty_option": 3, "uncovered_ink": 2},
            },
            {
                "created_at": now,
                "job_id": uuid4(),
                "pages_checked": 2,
                "errors": 0,
                "reviews": 2,
                "kinds": {},
            },
        ]

    def test_quality_report_shows_share_kinds_and_worst_jobs(
        self, tmp_path: Path
    ) -> None:
        rows = self._rows()
        (tmp_path / f"{rows[0]['job_id']}.json").write_text("{}")
        text = quality_report(rows, 7, answers_dir=tmp_path)
        assert "50" in text  # one of two jobs has errors
        assert text.index("empty_option") < text.index("uncovered_ink")
        assert "cavab faylı var" in text

    def test_quality_report_with_no_rows(self) -> None:
        assert "yoxdur" in quality_report([], 3)

    def test_usage_report_adds_the_kinds(self) -> None:
        row = {
            "created_at": datetime.now(timezone.utc),
            "pages": 1,
            "vision_calls": 3,
            "input_tokens": 10,
            "output_tokens": 5,
            "thought_tokens": 0,
            "est_cost_usd": 0.01,
            "duration_s": 3,
            "file_ext": "png",
            "outcome": "completed",
            "calls_by_kind": {"page": 1, "flowchart_rescue": 2},
        }
        text = usage_report([row], 1, 1.7)
        assert "flowchart_rescue=2" in text and "page=1" in text
        row["calls_by_kind"] = None  # an older row has no kinds
        assert "növ üzrə" not in usage_report([row], 1, 1.7)


# --------------------------------------------------------------------------
# Database (run in CI, where PostgreSQL is available)
# --------------------------------------------------------------------------


class TestQualityLogDatabase:
    def test_quality_and_kinds_are_stored_and_read_back(
        self, job_store: JobStore
    ) -> None:
        job_id = uuid4()
        job_store.record_quality(
            job_id=job_id,
            owner_user_id=None,
            pages_checked=3,
            errors=2,
            reviews=1,
            kinds={"empty_option": 2},
        )
        job_store.record_usage(
            job_id=job_id,
            owner_user_id=None,
            file_ext="png",
            pages=1,
            vision_calls=2,
            input_tokens=1,
            output_tokens=1,
            thought_tokens=0,
            est_cost_usd=0.001,
            duration_s=1.0,
            outcome="completed",
            calls_by_kind={"page": 1, "page_repeat": 1},
        )
        since = datetime.now(timezone.utc) - timedelta(1)
        quality = job_store.quality_rows_since(since)
        assert len(quality) == 1
        assert quality[0]["kinds"] == {"empty_option": 2}
        assert quality[0]["errors"] == 2 and quality[0]["pages_checked"] == 3
        usage = job_store.usage_rows_since(since)
        assert usage[0]["calls_by_kind"] == {"page": 1, "page_repeat": 1}

    def test_an_old_usage_row_has_no_kinds(self, job_store: JobStore) -> None:
        job_store.record_usage(
            job_id=uuid4(),
            owner_user_id=None,
            file_ext="png",
            pages=1,
            vision_calls=1,
            input_tokens=1,
            output_tokens=1,
            thought_tokens=0,
            est_cost_usd=0.001,
            duration_s=1.0,
            outcome="completed",
        )
        rows = job_store.usage_rows_since(datetime.now(timezone.utc) - timedelta(1))
        assert rows[0]["calls_by_kind"] is None


class TestWorkerLeavesAnswersAndQualityDatabase:
    def test_a_finished_job_saves_its_answers_and_a_quality_row(
        self,
        job_store: JobStore,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        page = options_with_pictures()
        source = tmp_path / "source.png"
        source.write_bytes(page.png)
        answers = tmp_path / "answers"
        monkeypatch.setattr("app.core.queue.worker.ANSWERS_DIR", answers)

        def pipeline(_path: str, out: Path, budget: VisionBudget) -> tuple[str, str]:
            report = _job_report.get()
            assert report is not None
            budget.consume()
            budget.add_usage(100, 10, None, kind="page")
            results = {
                result_key(page.png): VisionResult(
                    content_type=ContentType.PAGE, page=page.broken
                )
            }
            docx_path, pptx_path = run_pipeline(source, ReplayProvider(results), out)
            report.answers = results
            report.images = [page.png]
            report.docx_path = docx_path
            return str(docx_path), str(pptx_path)

        monkeypatch.setattr("app.core.queue.worker._process_job_sync", pipeline)
        job = job_store.create_job(
            job_id=uuid4(), input_filename="a.png", input_path=str(source)
        )

        async def run() -> None:
            task = asyncio.create_task(_worker_loop(job_store))
            for _ in range(60):  # until the quality row is written
                await asyncio.sleep(0.3)
                if job_store.quality_rows_since(
                    datetime.now(timezone.utc) - timedelta(1)
                ):
                    break
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run())

        assert job_store.get_job(job.id).status == JobStatus.COMPLETED  # type: ignore[union-attr]
        saved = answers / f"{job.id}.json"
        assert saved.exists()
        assert list(json.loads(saved.read_text(encoding="utf-8"))) == [
            result_key(page.png)
        ]
        quality = job_store.quality_rows_since(
            datetime.now(timezone.utc) - timedelta(1)
        )
        assert len(quality) == 1 and quality[0]["job_id"] == job.id
        assert quality[0]["kinds"]["empty_option"] == 5
        usage = job_store.usage_rows_since(datetime.now(timezone.utc) - timedelta(1))
        assert usage[0]["calls_by_kind"] == {"page": 1}
