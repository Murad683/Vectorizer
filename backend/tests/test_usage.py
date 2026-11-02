"""Usage log: token counting, the job_usage rows the worker writes, the report."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.config import get_settings
from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore
from app.core.queue.worker import _count_input_pages, _record_usage, _worker_loop
from app.core.vision import VisionBudget
from app.core.vision.gemini_provider import GeminiVisionProvider
from app.usage_report import build_report


class TestBudgetTokens:
    def test_tokens_add_up_and_none_counts_as_zero(self) -> None:
        budget = VisionBudget(limit=5)
        budget.add_usage(4000, 900, None)
        budget.add_usage(100, None, 50)
        assert (budget.input_tokens, budget.output_tokens) == (4100, 900)
        assert budget.thought_tokens == 50

    def test_tokens_never_limit_requests(self) -> None:
        budget = VisionBudget(limit=1)
        budget.add_usage(10**9, 10**9, 10**9)
        budget.consume()
        assert budget.used == 1


class TestProviderCountsTokens:
    @pytest.fixture()
    def provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[GeminiVisionProvider, VisionBudget]:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
        get_settings.cache_clear()
        monkeypatch.setattr("app.core.vision.gemini_provider.genai.Client", MagicMock())
        budget = VisionBudget(limit=5)
        return GeminiVisionProvider(budget), budget

    def test_real_numbers_reach_the_budget(
        self, provider: tuple[GeminiVisionProvider, VisionBudget]
    ) -> None:
        gemini, budget = provider
        response = MagicMock()
        response.usage_metadata.prompt_token_count = 4200
        response.usage_metadata.candidates_token_count = 900
        response.usage_metadata.thoughts_token_count = None
        gemini._note_usage(response, "page")
        assert (budget.input_tokens, budget.output_tokens) == (4200, 900)
        assert budget.thought_tokens == 0

    def test_a_mock_response_does_not_poison_the_counters(
        self, provider: tuple[GeminiVisionProvider, VisionBudget]
    ) -> None:
        gemini, budget = provider
        gemini._note_usage(MagicMock(), "page")  # numbers are MagicMocks
        assert (budget.input_tokens, budget.output_tokens) == (0, 0)


class _FakeStore:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self._fail = fail

    def record_usage(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("database is down")
        self.rows.append(kwargs)


def _job(path: Path) -> Job:
    return Job(
        id=uuid4(), owner_user_id=uuid4(), input_filename="x", input_path=str(path)
    )


class TestRecordUsage:
    def test_cost_pages_and_calls_of_this_attempt(self, tmp_path: Path) -> None:
        budget = VisionBudget(limit=25, used=3)  # 3 calls from an earlier attempt
        budget.consume()
        budget.consume()
        budget.add_usage(4000, 800, 200)
        store = _FakeStore()
        _record_usage(store, _job(tmp_path / "a.png"), budget, 3, 0.0, "completed")  # type: ignore[arg-type]
        row = store.rows[0]
        assert row["vision_calls"] == 2
        assert row["pages"] == 1 and row["file_ext"] == "png"
        assert row["outcome"] == "completed"
        # 4000 * 0.25/M + (800 + 200) * 1.50/M
        assert row["est_cost_usd"] == pytest.approx(0.0025)

    def test_a_broken_log_never_raises(self, tmp_path: Path) -> None:
        _record_usage(
            _FakeStore(fail=True),  # type: ignore[arg-type]
            _job(tmp_path / "a.png"),
            VisionBudget(limit=1),
            0,
            0.0,
            "failed",
        )

    def test_page_counts(self, tmp_path: Path) -> None:
        assert _count_input_pages(tmp_path / "a.docx") is None
        assert _count_input_pages(tmp_path / "missing.pdf") is None
        assert _count_input_pages(tmp_path / "b.JPG") == 1


def _run_worker_until_done(store: JobStore, job_id: object) -> None:
    async def run() -> None:
        task = asyncio.create_task(_worker_loop(store))
        for _ in range(40):
            await asyncio.sleep(0.3)
            job = store.get_job(job_id)  # type: ignore[arg-type]
            if job and job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())


class TestUsageRowsInTheDatabase:
    def test_the_worker_writes_one_row_for_a_finished_job(
        self, job_store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def pipeline(_path: str, _out: Path, budget: VisionBudget) -> tuple[str, str]:
            budget.consume()
            budget.add_usage(4000, 1000, None)
            return "a.docx", "a.pptx"

        monkeypatch.setattr("app.core.queue.worker._process_job_sync", pipeline)
        job = job_store.create_job(
            job_id=uuid4(),
            input_filename="secret-name.png",
            input_path=str(tmp_path / "a.png"),
            owner_user_id=None,
        )
        _run_worker_until_done(job_store, job.id)

        rows = job_store.usage_rows_since(datetime.now(timezone.utc) - timedelta(1))
        assert len(rows) == 1
        row = rows[0]
        assert row["job_id"] == job.id and row["outcome"] == "completed"
        assert row["vision_calls"] == 1 and row["input_tokens"] == 4000
        assert row["file_ext"] == "png"
        # numbers only: no file name or text is stored anywhere in the row
        assert "secret-name" not in str(dict(row))

    def test_a_failed_job_is_logged_too(
        self, job_store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def pipeline(_path: str, _out: Path, budget: VisionBudget) -> None:
            budget.consume()
            raise RuntimeError("boom")

        monkeypatch.setattr("app.core.queue.worker._process_job_sync", pipeline)
        job = job_store.create_job(
            job_id=uuid4(), input_filename="a.png", input_path=str(tmp_path / "a.png")
        )
        job_store.update_job(job.id, retry_count=2)  # no retries left
        _run_worker_until_done(job_store, job.id)

        rows = job_store.usage_rows_since(datetime.now(timezone.utc) - timedelta(1))
        assert [r["outcome"] for r in rows] == ["failed"]

    def test_a_broken_usage_log_does_not_fail_the_job(
        self, job_store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "app.core.queue.worker._process_job_sync",
            lambda *_a: ("a.docx", "a.pptx"),
        )

        def broken(**_k: object) -> None:
            raise RuntimeError("log table missing")

        monkeypatch.setattr(job_store, "record_usage", broken)
        job = job_store.create_job(
            job_id=uuid4(), input_filename="a.png", input_path=str(tmp_path / "a.png")
        )
        _run_worker_until_done(job_store, job.id)
        final = job_store.get_job(job.id)
        assert final is not None and final.status == JobStatus.COMPLETED

    def test_the_log_survives_deleting_the_job(
        self, job_store: JobStore, tmp_path: Path
    ) -> None:
        job = job_store.create_job(
            job_id=uuid4(), input_filename="a.png", input_path=str(tmp_path / "a.png")
        )
        job_store.record_usage(
            job_id=job.id,
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
        job_store.delete_job(job.id)
        rows = job_store.usage_rows_since(datetime.now(timezone.utc) - timedelta(1))
        assert len(rows) == 1


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "created_at": datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc),
        "file_ext": "pdf",
        "pages": 4,
        "vision_calls": 5,
        "input_tokens": 20000,
        "output_tokens": 4000,
        "thought_tokens": 0,
        "est_cost_usd": 0.011,
        "duration_s": 30.0,
        "outcome": "completed",
    }
    base.update(over)
    return base


class TestReport:
    def test_empty_log(self) -> None:
        assert "məlumat yoxdur" in build_report([], 7, 1.70)

    def test_totals_average_and_top_rows(self) -> None:
        rows = [_row(), _row(pages=None, est_cost_usd=0.05, outcome="failed")]
        text = build_report(rows, 7, 2.0)
        assert "2026-11-03" in text
        assert "cəmi" in text
        assert "0.0610" in text  # total USD
        assert "0.122" in text  # total AZN at rate 2.0
        assert "failed=1" in text and "completed=1" in text
        assert text.index("0.0500 USD") < text.index("0.0110 USD")  # dearest first
