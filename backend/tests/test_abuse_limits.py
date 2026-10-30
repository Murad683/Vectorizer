"""Abuse and cost limits: page cap, per-user job limits, Gemini ceiling, login."""

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.auth.models import Role
from app.core.auth.security import hash_password
from app.core.auth.store import RefreshTokenStore, UserStore
from app.core.ingestion import count_pdf_pages
from app.core.queue.models import JobStatus
from app.core.queue.store import JobStore
from app.core.queue.worker import _worker_loop
from app.core.vision import VisionBudget, VisionBudgetExceededError
from app.core.vision.gemini_provider import GeminiVisionProvider
from app.main import create_app

NGINX_CONF = Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Set env vars and drop the cached settings so the app re-reads them."""
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return bytes(data)


# --------------------------------------------------------------------------
# VisionBudget and the Gemini provider (no database needed)
# --------------------------------------------------------------------------


class TestVisionBudget:
    def test_consume_until_limit_then_raise(self) -> None:
        budget = VisionBudget(limit=2)
        budget.consume()
        budget.consume()
        with pytest.raises(VisionBudgetExceededError):
            budget.consume()
        assert budget.used == 2

    def test_requests_from_earlier_attempts_count(self) -> None:
        budget = VisionBudget(limit=5, used=5)
        with pytest.raises(VisionBudgetExceededError):
            budget.consume()


class _FakeModels:
    def __init__(self, response: object) -> None:
        self.calls = 0
        self._response = response

    def generate_content(self, **_: object) -> object:
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _provider(
    monkeypatch: pytest.MonkeyPatch, budget: VisionBudget, response: object
) -> tuple[GeminiVisionProvider, _FakeModels]:
    _set_env(monkeypatch, GEMINI_API_KEY="test-key")
    monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", lambda _: None)
    provider = GeminiVisionProvider(budget=budget)
    models = _FakeModels(response)
    provider._client = SimpleNamespace(models=models)  # type: ignore[assignment]
    return provider, models


class TestGeminiProviderBudget:
    def test_invalid_json_retries_stop_at_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, flowchart_png_bytes: bytes
    ) -> None:
        budget = VisionBudget(limit=2)
        provider, models = _provider(
            monkeypatch, budget, SimpleNamespace(text="not json")
        )
        with pytest.raises(VisionBudgetExceededError):
            provider.analyze_diagram(flowchart_png_bytes)
        assert models.calls == 2

    def test_transient_errors_stop_at_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, flowchart_png_bytes: bytes
    ) -> None:
        budget = VisionBudget(limit=1)
        provider, models = _provider(monkeypatch, budget, ConnectionError("down"))
        with pytest.raises(VisionBudgetExceededError):
            provider.analyze_diagram(flowchart_png_bytes)
        assert models.calls == 1

    def test_topology_retry_is_counted_and_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        budget = VisionBudget(limit=1, used=1)
        provider, models = _provider(monkeypatch, budget, SimpleNamespace(text="{}"))
        with pytest.raises(VisionBudgetExceededError):
            provider._retry_topology(["prompt"], ["a loop-back arrow is missing"])
        assert models.calls == 0


# --------------------------------------------------------------------------
# PDF page counting and the nginx rules (no database needed)
# --------------------------------------------------------------------------


class TestPdfPageCount:
    def test_counts_pages(self) -> None:
        assert count_pdf_pages(_pdf(3)) == 3

    def test_unreadable_pdf_returns_none(self) -> None:
        assert count_pdf_pages(b"this is not a pdf") is None


class TestNginxConfig:
    """Guards the rules that cannot be exercised without running nginx."""

    def test_login_is_rate_limited_per_real_client_ip(self) -> None:
        conf = NGINX_CONF.read_text(encoding="utf-8")
        assert "$http_cf_connecting_ip" in conf
        assert "limit_req_zone $client_ip zone=login:1m rate=10r/m" in conf
        assert "limit_req_status 429" in conf
        assert "limit_req zone=login" in conf

    def test_api_docs_and_schema_are_blocked(self) -> None:
        conf = NGINX_CONF.read_text(encoding="utf-8")
        assert r"^/api/(docs|redoc|openapi\.json)" in conf
        assert "return 404" in conf


# --------------------------------------------------------------------------
# Worker: ceiling fails the job without a retry (database)
# --------------------------------------------------------------------------


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


class TestWorkerCeiling:
    def test_budget_exceeded_fails_without_retry(
        self, job_store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def pipeline(_path: str, _out: Path, budget: VisionBudget) -> None:
            budget.consume()
            raise VisionBudgetExceededError("limit")

        monkeypatch.setattr("app.core.queue.worker._process_job_sync", pipeline)
        job = job_store.create_job(
            job_id=uuid4(),
            input_filename="a.png",
            input_path=str(tmp_path / "a.png"),
        )
        _run_worker_until_done(job_store, job.id)

        final = job_store.get_job(job.id)
        assert final is not None
        assert final.status == JobStatus.FAILED
        assert final.retry_count == 0
        assert final.vision_calls == 1
        assert final.error_message is not None
        assert "sorğu" in final.error_message
        assert "Traceback" not in final.error_message

    def test_a_retry_cannot_restart_the_allowance(
        self, job_store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _set_env(monkeypatch, MAX_VISION_CALLS_PER_JOB="3")

        def pipeline(_path: str, _out: Path, budget: VisionBudget) -> None:
            budget.consume()
            budget.consume()
            raise RuntimeError("Gemini returned garbage")

        monkeypatch.setattr("app.core.queue.worker._process_job_sync", pipeline)
        job = job_store.create_job(
            job_id=uuid4(),
            input_filename="a.png",
            input_path=str(tmp_path / "a.png"),
        )
        _run_worker_until_done(job_store, job.id)

        final = job_store.get_job(job.id)
        assert final is not None
        assert final.status == JobStatus.FAILED
        # Attempt 1 spent 2 and was retried; attempt 2 hit the ceiling of 3
        # on its second request and was not retried again.
        assert final.retry_count == 1
        assert final.vision_calls == 3


class TestInterruptedJobs:
    def test_processing_jobs_are_requeued_after_a_restart(
        self, job_store: JobStore, tmp_path: Path
    ) -> None:
        job = job_store.create_job(
            job_id=uuid4(),
            input_filename="a.png",
            input_path=str(tmp_path / "a.png"),
        )
        assert job_store.claim_next_pending_job() is not None  # now PROCESSING

        assert job_store.recover_interrupted_jobs() == 1

        again = job_store.get_job(job.id)
        assert again is not None
        assert again.status == JobStatus.PENDING
        assert again.retry_count == 1

    def test_a_job_that_keeps_interrupting_ends_up_failed(
        self, job_store: JobStore, tmp_path: Path
    ) -> None:
        job = job_store.create_job(
            job_id=uuid4(),
            input_filename="a.png",
            input_path=str(tmp_path / "a.png"),
        )
        job_store.update_job(job.id, status=JobStatus.PROCESSING, retry_count=2)

        job_store.recover_interrupted_jobs()

        final = job_store.get_job(job.id)
        assert final is not None
        assert final.status == JobStatus.FAILED


# --------------------------------------------------------------------------
# Upload and login limits through the API (database)
# --------------------------------------------------------------------------


@asynccontextmanager
async def _noop_lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture()
def stores(
    job_store: JobStore, user_store: UserStore, refresh_token_store: RefreshTokenStore
) -> tuple[JobStore, UserStore, RefreshTokenStore]:
    return job_store, user_store, refresh_token_store


@pytest.fixture()
def client(
    stores: tuple[JobStore, UserStore, RefreshTokenStore],
) -> TestClient:
    job_store, user_store, refresh_token_store = stores
    accounts = (("admin@test.com", Role.ADMIN), ("member@test.com", Role.MEMBER))
    for email, role in accounts:
        user_store.create_user(
            user_id=uuid4(),
            email=email,
            hashed_password=hash_password("secret-pass"),
            role=role,
        )
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.job_store = job_store
    app.state.user_store = user_store
    app.state.refresh_token_store = refresh_token_store
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(client: TestClient, email: str = "admin@test.com") -> dict[str, str]:
    resp = client.post("/auth/login", json={"email": email, "password": "secret-pass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(
    client: TestClient, headers: dict[str, str], name: str = "a.png", data: bytes = b"x"
) -> object:
    files = {"file": (name, data, "image/png")}
    return client.post("/jobs", files=files, headers=headers)


def _finish_all(store: JobStore) -> None:
    for job in store.list_jobs():
        store.update_job(job.id, status=JobStatus.COMPLETED)


class TestPageLimit:
    def test_pdf_over_the_limit_is_rejected_with_a_clear_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, MAX_PAGES_PER_FILE="3")
        headers = _login(client)
        resp = _upload(client, headers, "big.pdf", _pdf(4))
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "too_many_pages"
        assert "4 səhifə" in body["message"] and "3 səhifə" in body["message"]

    def test_pdf_at_the_limit_is_accepted(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, MAX_PAGES_PER_FILE="3")
        resp = _upload(client, _login(client), "ok.pdf", _pdf(3))
        assert resp.status_code == 201


class TestJobLimits:
    def test_active_job_limit(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, MAX_ACTIVE_JOBS_PER_USER="2")
        headers = _login(client)
        assert _upload(client, headers).status_code == 201
        assert _upload(client, headers).status_code == 201
        blocked = _upload(client, headers)
        assert blocked.status_code == 429
        assert blocked.json()["error_code"] == "too_many_active_jobs"
        assert "ən çox 2 iş" in blocked.json()["message"]
        # Finished jobs free the slots again.
        _finish_all(stores[0])
        assert _upload(client, headers).status_code == 201

    def test_limit_is_per_user(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, MAX_ACTIVE_JOBS_PER_USER="1")
        admin = _login(client)
        member = _login(client, "member@test.com")
        assert _upload(client, admin).status_code == 201
        assert _upload(client, admin).status_code == 429
        assert _upload(client, member).status_code == 201

    def test_daily_limit_survives_deleting_jobs(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(
            monkeypatch,
            MAX_JOBS_PER_USER_PER_DAY="2",
            MAX_ACTIVE_JOBS_PER_USER="10",
        )
        job_store = stores[0]
        headers = _login(client)
        first = _upload(client, headers)
        assert first.status_code == 201
        assert _upload(client, headers).status_code == 201
        blocked = _upload(client, headers)
        assert blocked.status_code == 429
        assert blocked.json()["error_code"] == "daily_job_limit"
        assert "24 saat" in blocked.json()["message"]

        # Deleting a job must not give the quota back.
        deleted = client.delete(f"/jobs/{first.json()['job_id']}", headers=headers)
        assert deleted.status_code == 204
        assert _upload(client, headers).status_code == 429

        # Submissions older than 24 hours no longer count.
        job_store._conn.execute(
            "UPDATE job_submissions SET created_at = now() - interval '25 hours'"
        )
        assert _upload(client, headers).status_code == 201


class TestLogin:
    def test_bcrypt_runs_off_the_event_loop(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.api.routes.auth as auth_routes

        seen: list[str] = []
        real = auth_routes.verify_password

        def spy(plain: str, hashed: str) -> bool:
            seen.append(threading.current_thread().name)
            return real(plain, hashed)

        monkeypatch.setattr(auth_routes, "verify_password", spy)
        _login(client)
        # The app runs on the TestClient's portal thread; bcrypt must be
        # handed to a worker thread instead of blocking that loop.
        assert seen and all("worker" in name.lower() for name in seen)

    def test_unknown_email_costs_a_hash_check_and_is_rejected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.api.routes.auth as auth_routes

        calls: list[str] = []
        monkeypatch.setattr(
            auth_routes, "verify_password_dummy", lambda plain: calls.append(plain)
        )
        resp = client.post(
            "/auth/login", json={"email": "nobody@test.com", "password": "whatever"}
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "invalid_credentials"
        assert calls == ["whatever"]
