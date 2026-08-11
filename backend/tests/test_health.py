"""Tests for the health endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    """GET /health responds with 200 and the expected payload."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_sets_request_id_header() -> None:
    """Every response carries an X-Request-ID correlation header."""
    response = client.get("/health")
    assert response.headers.get("X-Request-ID")
