"""Tests for authentication, authorization, and user management (Phase 7)."""

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
from app.core.queue.store import JobStore
from app.main import create_app


@asynccontextmanager
async def _noop_lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture()
def _stores(job_store: JobStore, user_store: UserStore):
    return job_store, user_store


@pytest.fixture()
def client(_stores) -> TestClient:
    job_store, user_store = _stores
    user_store.create_user(
        user_id=uuid4(),
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


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    return resp.json(), resp.status_code


def _token(
    client: TestClient, email: str = "admin@test.com", password: str = "adminpass"
) -> str:
    body, status = _login(client, email, password)
    assert status == 200
    return body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestLogin:
    def test_valid_login(self, client: TestClient) -> None:
        body, status = _login(client, "admin@test.com", "adminpass")
        assert status == 200
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    def test_wrong_password_401(self, client: TestClient) -> None:
        body, status = _login(client, "admin@test.com", "wrong")
        assert status == 401
        assert body["error_code"] == "invalid_credentials"

    def test_nonexistent_email_401(self, client: TestClient) -> None:
        body, status = _login(client, "nobody@test.com", "whatever")
        assert status == 401
        assert body["error_code"] == "invalid_credentials"

    def test_wrong_password_same_message_as_wrong_email(
        self, client: TestClient
    ) -> None:
        _, status1 = _login(client, "admin@test.com", "wrong")
        body1, _ = _login(client, "admin@test.com", "wrong")
        body2, status2 = _login(client, "nobody@test.com", "whatever")
        assert status1 == status2 == 401
        assert body1["message"] == body2["message"]


class TestUserManagement:
    def test_admin_creates_member(self, client: TestClient) -> None:
        token = _token(client)
        resp = client.post(
            "/users",
            json={"email": "member@test.com", "password": "memberpass"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == "member@test.com"
        assert body["role"] == "member"
        assert "hashed_password" not in body

    def test_member_cannot_create_user(self, client: TestClient) -> None:
        admin_token = _token(client)
        client.post(
            "/users",
            json={"email": "member@test.com", "password": "memberpass"},
            headers=_auth(admin_token),
        )
        member_token = _token(client, "member@test.com", "memberpass")
        resp = client.post(
            "/users",
            json={"email": "another@test.com", "password": "pass"},
            headers=_auth(member_token),
        )
        assert resp.status_code == 403

    def test_member_cannot_list_users(self, client: TestClient) -> None:
        admin_token = _token(client)
        client.post(
            "/users",
            json={"email": "member@test.com", "password": "memberpass"},
            headers=_auth(admin_token),
        )
        member_token = _token(client, "member@test.com", "memberpass")
        resp = client.get("/users", headers=_auth(member_token))
        assert resp.status_code == 403

    def test_admin_lists_users(self, client: TestClient) -> None:
        token = _token(client)
        resp = client.get("/users", headers=_auth(token))
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) >= 1
        assert all("hashed_password" not in u for u in users)

    def test_duplicate_email_409(self, client: TestClient) -> None:
        token = _token(client)
        client.post(
            "/users",
            json={"email": "dup@test.com", "password": "pass1"},
            headers=_auth(token),
        )
        resp = client.post(
            "/users",
            json={"email": "dup@test.com", "password": "pass2"},
            headers=_auth(token),
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "email_already_exists"


class TestTokenValidation:
    def test_no_token_401(self, client: TestClient) -> None:
        resp = client.get("/jobs/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 401

    def test_invalid_token_401(self, client: TestClient) -> None:
        resp = client.get(
            "/jobs/00000000-0000-0000-0000-000000000000",
            headers=_auth("not.a.valid.token"),
        )
        assert resp.status_code == 401

    def test_expired_token_401(self, client: TestClient) -> None:
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.config import get_settings

        payload = {
            "sub": str(uuid4()),
            "role": "admin",
            "iat": datetime.now(timezone.utc) - timedelta(hours=48),
            "exp": datetime.now(timezone.utc) - timedelta(hours=24),
        }
        expired = jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")
        resp = client.get(
            "/jobs/00000000-0000-0000-0000-000000000000",
            headers=_auth(expired),
        )
        assert resp.status_code == 401


class TestBootstrapAdmin:
    def test_bootstrap_creates_admin(self, user_store: UserStore) -> None:
        assert not user_store.has_any_admin()
        from app.main import _bootstrap_admin

        with patch("app.main.get_settings") as mock:
            s = MagicMock()
            s.admin_email = "boot@test.com"
            s.admin_password = "bootpass"
            mock.return_value = s
            _bootstrap_admin(user_store)

        assert user_store.has_any_admin()
        user = user_store.get_user_by_email("boot@test.com")
        assert user is not None
        assert user.role == Role.ADMIN

    def test_bootstrap_is_idempotent(self, user_store: UserStore) -> None:
        from app.main import _bootstrap_admin

        with patch("app.main.get_settings") as mock:
            s = MagicMock()
            s.admin_email = "boot@test.com"
            s.admin_password = "bootpass"
            mock.return_value = s
            _bootstrap_admin(user_store)
            _bootstrap_admin(user_store)

        users = user_store.list_users()
        admins = [u for u in users if u.role == Role.ADMIN]
        assert len(admins) == 1


class TestJobOwnership:
    @pytest.fixture()
    def sample_bytes(self, sample_docx: Path) -> bytes:
        return sample_docx.read_bytes()

    def _create_member(self, client: TestClient, admin_token: str) -> str:
        client.post(
            "/users",
            json={"email": "member@test.com", "password": "memberpass"},
            headers=_auth(admin_token),
        )
        return _token(client, "member@test.com", "memberpass")

    def test_member_sees_own_job(self, client: TestClient, sample_bytes: bytes) -> None:
        admin_token = _token(client)
        member_token = self._create_member(client, admin_token)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(member_token),
        )
        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        resp = client.get(f"/jobs/{job_id}", headers=_auth(member_token))
        assert resp.status_code == 200

    def test_member_cannot_see_other_job(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        admin_token = _token(client)
        member_token = self._create_member(client, admin_token)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 201
        admin_job_id = resp.json()["job_id"]
        resp = client.get(f"/jobs/{admin_job_id}", headers=_auth(member_token))
        assert resp.status_code == 403

    def test_admin_sees_any_job(self, client: TestClient, sample_bytes: bytes) -> None:
        admin_token = _token(client)
        member_token = self._create_member(client, admin_token)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(member_token),
        )
        assert resp.status_code == 201
        member_job_id = resp.json()["job_id"]
        resp = client.get(f"/jobs/{member_job_id}", headers=_auth(admin_token))
        assert resp.status_code == 200

    def test_member_cannot_download_other_job(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        admin_token = _token(client)
        member_token = self._create_member(client, admin_token)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(admin_token),
        )
        admin_job_id = resp.json()["job_id"]
        resp = client.get(
            f"/jobs/{admin_job_id}/download?format=docx",
            headers=_auth(member_token),
        )
        assert resp.status_code == 403

    def test_member_cannot_delete_other_job(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        admin_token = _token(client)
        member_token = self._create_member(client, admin_token)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(admin_token),
        )
        admin_job_id = resp.json()["job_id"]
        resp = client.delete(f"/jobs/{admin_job_id}", headers=_auth(member_token))
        assert resp.status_code == 403
        # Confirm it's still there.
        resp = client.get(f"/jobs/{admin_job_id}", headers=_auth(admin_token))
        assert resp.status_code == 200

    def test_admin_can_delete_member_job(
        self, client: TestClient, sample_bytes: bytes
    ) -> None:
        admin_token = _token(client)
        member_token = self._create_member(client, admin_token)
        resp = client.post(
            "/jobs",
            files={"file": ("test.docx", sample_bytes, "application/octet-stream")},
            headers=_auth(member_token),
        )
        member_job_id = resp.json()["job_id"]
        resp = client.delete(f"/jobs/{member_job_id}", headers=_auth(admin_token))
        assert resp.status_code == 204
        resp = client.get(f"/jobs/{member_job_id}", headers=_auth(member_token))
        assert resp.status_code == 404


class TestUserDeactivation:
    def _create_member(self, client: TestClient, admin_token: str) -> str:
        client.post(
            "/users",
            json={"email": "member@test.com", "password": "memberpass"},
            headers=_auth(admin_token),
        )
        resp = client.get("/users", headers=_auth(admin_token))
        member = next(u for u in resp.json() if u["email"] == "member@test.com")
        return str(member["id"])

    def test_admin_deactivates_member(self, client: TestClient) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)

        resp = client.patch(
            f"/users/{member_id}",
            json={"is_active": False},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    def test_deactivated_user_cannot_login(self, client: TestClient) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        client.patch(
            f"/users/{member_id}",
            json={"is_active": False},
            headers=_auth(admin_token),
        )
        body, status = _login(client, "member@test.com", "memberpass")
        assert status == 401
        assert body["error_code"] == "invalid_credentials"

    def test_deactivation_does_not_touch_job_history(
        self, client: TestClient, sample_docx: Path
    ) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        member_token = _token(client, "member@test.com", "memberpass")

        create_resp = client.post(
            "/jobs",
            files={
                "file": (
                    "test.docx",
                    sample_docx.read_bytes(),
                    "application/octet-stream",
                )
            },
            headers=_auth(member_token),
        )
        job_id = create_resp.json()["job_id"]

        client.patch(
            f"/users/{member_id}",
            json={"is_active": False},
            headers=_auth(admin_token),
        )

        # Job still visible to the admin, untouched.
        resp = client.get(f"/jobs/{job_id}", headers=_auth(admin_token))
        assert resp.status_code == 200

    def test_reactivating_restores_login(self, client: TestClient) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        client.patch(
            f"/users/{member_id}",
            json={"is_active": False},
            headers=_auth(admin_token),
        )
        client.patch(
            f"/users/{member_id}",
            json={"is_active": True},
            headers=_auth(admin_token),
        )
        _, status = _login(client, "member@test.com", "memberpass")
        assert status == 200

    def test_member_cannot_deactivate_others(self, client: TestClient) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        member_token = _token(client, "member@test.com", "memberpass")
        resp = client.patch(
            f"/users/{member_id}",
            json={"is_active": False},
            headers=_auth(member_token),
        )
        assert resp.status_code == 403

    def test_admin_cannot_deactivate_self(self, client: TestClient) -> None:
        admin_token = _token(client)
        resp = client.get("/users", headers=_auth(admin_token))
        admin_id = next(u["id"] for u in resp.json() if u["email"] == "admin@test.com")
        resp = client.patch(
            f"/users/{admin_id}",
            json={"is_active": False},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "cannot_modify_self"

    def test_deactivate_nonexistent_user_404(self, client: TestClient) -> None:
        admin_token = _token(client)
        resp = client.patch(
            "/users/00000000-0000-0000-0000-000000000000",
            json={"is_active": False},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 404


class TestUserDeletion:
    def _create_member(self, client: TestClient, admin_token: str) -> str:
        client.post(
            "/users",
            json={"email": "member@test.com", "password": "memberpass"},
            headers=_auth(admin_token),
        )
        resp = client.get("/users", headers=_auth(admin_token))
        member = next(u for u in resp.json() if u["email"] == "member@test.com")
        return str(member["id"])

    def test_delete_user_with_no_jobs_succeeds(self, client: TestClient) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        resp = client.delete(f"/users/{member_id}", headers=_auth(admin_token))
        assert resp.status_code == 204
        resp = client.get("/users", headers=_auth(admin_token))
        assert all(u["id"] != member_id for u in resp.json())

    def test_delete_user_with_jobs_blocked(
        self, client: TestClient, sample_docx: Path
    ) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        member_token = _token(client, "member@test.com", "memberpass")
        client.post(
            "/jobs",
            files={
                "file": (
                    "test.docx",
                    sample_docx.read_bytes(),
                    "application/octet-stream",
                )
            },
            headers=_auth(member_token),
        )

        resp = client.delete(f"/users/{member_id}", headers=_auth(admin_token))
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "user_owns_jobs"

        # User must still exist.
        resp = client.get("/users", headers=_auth(admin_token))
        assert any(u["id"] == member_id for u in resp.json())

    def test_member_cannot_delete_users(self, client: TestClient) -> None:
        admin_token = _token(client)
        member_id = self._create_member(client, admin_token)
        member_token = _token(client, "member@test.com", "memberpass")
        resp = client.delete(f"/users/{member_id}", headers=_auth(member_token))
        assert resp.status_code == 403

    def test_admin_cannot_delete_self(self, client: TestClient) -> None:
        admin_token = _token(client)
        resp = client.get("/users", headers=_auth(admin_token))
        admin_id = next(u["id"] for u in resp.json() if u["email"] == "admin@test.com")
        resp = client.delete(f"/users/{admin_id}", headers=_auth(admin_token))
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "cannot_modify_self"

    def test_delete_nonexistent_user_404(self, client: TestClient) -> None:
        admin_token = _token(client)
        resp = client.delete(
            "/users/00000000-0000-0000-0000-000000000000",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 404
