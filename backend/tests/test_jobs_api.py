"""API-level tests for the /jobs endpoints (with auth)."""

import io
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth.models import Role
from app.core.auth.security import hash_password
from app.core.auth.store import UserStore
from app.core.queue.models import JobStatus
from app.core.queue.store import JobStore
from app.main import create_app


@asynccontextmanager
async def _noop_lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture()
def _stores(job_store: JobStore, user_store: UserStore):
    return job_store, user_store


@pytest.fixture()
def client(_stores, tmp_path: Path) -> TestClient:
    job_store, user_store = _stores
    admin_id = uuid4()
    user_store.create_user(
        user_id=admin_id,
        email="admin@test.com",
        hashed_password=hash_password("adminpass"),
        role=Role.ADMIN,
    )
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.job_store = job_store
    app.state.user_store = user_store
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(
    client: TestClient, email: str = "admin@test.com", password: str = "adminpass"
) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def sample_bytes(sample_docx: Path) -> bytes:
    return sample_docx.read_bytes()


class TestUploadValidation:
    def test_unsupported_format_rejected(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.post(
            "/jobs",
            files={"file": ("test.txt", b"hello", "text/plain")},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "unsupported_format"

    def test_missing_filename_rejected(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.post(
            "/jobs",
            files={"file": ("", b"hello", "application/octet-stream")},
            headers=_auth(token),
        )
        assert resp.status_code in (400, 422)

    def test_oversized_file_rejected(self, client: TestClient) -> None:
        token = _login(client)
        with patch("app.api.routes.jobs.get_settings") as mock_settings:
            settings = MagicMock()
            settings.max_upload_size_mb = 0
            mock_settings.return_value = settings
            resp = client.post(
                "/jobs",
                files={"file": ("test.docx", b"x" * 100, "application/octet-stream")},
                headers=_auth(token),
            )
        assert resp.status_code == 413
        assert resp.json()["error_code"] == "file_too_large"

    def test_unauthenticated_upload_401(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", b"hello", "application/octet-stream")},
        )
        assert resp.status_code == 401


class TestJobLifecycle:
    def test_create_returns_pending(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        token = _login(client)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "pending"
        assert "job_id" in body

    def test_get_status_pending(self, client: TestClient, sample_bytes: bytes) -> None:
        token = _login(client)
        create_resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        job_id = create_resp.json()["job_id"]
        resp = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    def test_get_nonexistent_job_404(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.get(
            "/jobs/00000000-0000-0000-0000-000000000000",
            headers=_auth(token),
        )
        assert resp.status_code == 404

    def test_download_before_completion_409(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        token = _login(client)
        create_resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        job_id = create_resp.json()["job_id"]
        resp = client.get(f"/jobs/{job_id}/download?format=docx", headers=_auth(token))
        assert resp.status_code == 409

    def test_download_invalid_format_400(
        self, client: TestClient, sample_bytes: bytes, tmp_path: Path
    ) -> None:
        token = _login(client)
        create_resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        job_id = create_resp.json()["job_id"]
        from uuid import UUID

        store = client.app.state.job_store  # type: ignore[union-attr]
        store.update_job(UUID(job_id), status=JobStatus.COMPLETED)
        resp = client.get(f"/jobs/{job_id}/download?format=xlsx", headers=_auth(token))
        assert resp.status_code == 400

    def test_completed_job_download(
        self, client: TestClient, sample_bytes: bytes, tmp_path: Path
    ) -> None:
        token = _login(client)
        create_resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        job_id = create_resp.json()["job_id"]
        from uuid import UUID

        import docx

        out_dir = tmp_path / "output"
        out_dir.mkdir()
        docx_path = out_dir / "output.docx"
        doc = docx.Document()
        doc.add_paragraph("test")
        doc.save(str(docx_path))

        store = client.app.state.job_store  # type: ignore[union-attr]
        store.update_job(
            UUID(job_id),
            status=JobStatus.COMPLETED,
            output_docx_path=str(docx_path),
        )

        resp = client.get(f"/jobs/{job_id}/download?format=docx", headers=_auth(token))
        assert resp.status_code == 200
        assert "wordprocessingml" in resp.headers["content-type"]
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "word/document.xml" in zf.namelist()


class TestJobDeletion:
    def test_owner_can_delete_own_job(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        token = _login(client)
        create_resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        job_id = create_resp.json()["job_id"]

        resp = client.delete(f"/jobs/{job_id}", headers=_auth(token))
        assert resp.status_code == 204

        resp = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert resp.status_code == 404

    def test_delete_removes_files_from_disk(
        self, client: TestClient, sample_bytes: bytes, tmp_path: Path
    ) -> None:
        token = _login(client)
        create_resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(token),
        )
        job_id = create_resp.json()["job_id"]

        from uuid import UUID

        import docx

        out_dir = tmp_path / "output"
        out_dir.mkdir()
        docx_path = out_dir / "output.docx"
        docx.Document().save(str(docx_path))

        store = client.app.state.job_store  # type: ignore[union-attr]
        store.update_job(
            UUID(job_id),
            status=JobStatus.COMPLETED,
            output_docx_path=str(docx_path),
        )
        assert docx_path.exists()

        resp = client.delete(f"/jobs/{job_id}", headers=_auth(token))
        assert resp.status_code == 204
        assert not docx_path.exists()

    def test_delete_nonexistent_job_404(self, client: TestClient) -> None:
        token = _login(client)
        resp = client.delete(
            "/jobs/00000000-0000-0000-0000-000000000000", headers=_auth(token)
        )
        assert resp.status_code == 404

    def test_unauthenticated_delete_401(self, client: TestClient) -> None:
        resp = client.delete("/jobs/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 401
