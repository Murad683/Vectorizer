"""Freezes two error responses so a refactor cannot change them unnoticed."""

import asyncio
import io
import json
from types import SimpleNamespace

from fastapi import UploadFile
from fastapi.security import HTTPAuthorizationCredentials

from app.api.routes.jobs import create_job
from app.core.auth.dependencies import _AuthError, get_current_user


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(request_id="rid"),
        app=SimpleNamespace(state=SimpleNamespace(job_store=None)),
    )


def test_unsupported_format_message_is_byte_for_byte_stable() -> None:
    upload = UploadFile(filename="notes.txt", file=io.BytesIO(b""))
    resp = asyncio.run(
        create_job(file=upload, request=_request(), current_user=None)  # type: ignore[arg-type]
    )
    assert resp.status_code == 400
    assert json.loads(resp.body) == {
        "error_code": "unsupported_format",
        "message": (
            "Unsupported file type '.txt'. " "Supported: .docx, .jpeg, .jpg, .pdf, .png"
        ),
        "request_id": "rid",
    }


def test_invalid_token_still_answers_401_without_exception_chaining() -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="garbage")
    try:
        asyncio.run(get_current_user(_request(), credentials))  # type: ignore[arg-type]
    except _AuthError as exc:
        assert exc.response.status_code == 401
        assert json.loads(exc.response.body) == {
            "error_code": "invalid_token",
            "message": "Token is invalid or expired.",
            "request_id": None,
        }
        # `raise ... from None`: only the traceback display changes.
        assert exc.__suppress_context__ is True
        assert exc.__cause__ is None
    else:
        raise AssertionError("expected _AuthError")
