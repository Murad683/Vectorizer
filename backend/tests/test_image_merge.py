"""Several images merged into one document (one Word, one PowerPoint).

Offline part: the image set reader, the request ceiling, the whole pipeline with
a fake vision model. The ``...Database`` classes need PostgreSQL and run in CI.
"""

import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.core import vision as vision_pkg
from app.core.auth.models import Role
from app.core.auth.security import hash_password
from app.core.auth.store import RefreshTokenStore, UserStore
from app.core.ingestion import (
    ImageSetExtractor,
    get_extractor,
    image_names,
)
from app.core.queue.models import JobStatus
from app.core.queue.store import JobStore
from app.core.queue.worker import _count_input_pages, _process_job_sync, _vision_limit
from app.core.vision import VisionBudget
from app.core.vision.base import VisionProvider
from app.main import create_app
from app.models.document import ContentType, ElementKind, VisionResult
from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun


def _image(color: str, fmt: str = "PNG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (300, 400), color).save(buffer, format=fmt)
    return buffer.getvalue()


_RED, _GREEN, _BLUE = _image("red"), _image("green", "JPEG"), _image("blue")


def _zip(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return path


def _set(tmp_path: Path) -> Path:
    return _zip(
        tmp_path / "set.zip",
        [("0001.png", _RED), ("0002.jpg", _GREEN), ("0003.png", _BLUE)],
    )


class TestImageSetExtractor:
    def test_one_whole_page_element_per_image_in_order(self, tmp_path: Path) -> None:
        elements = ImageSetExtractor().extract(_set(tmp_path))
        assert [e.source_page for e in elements] == [1, 2, 3]
        assert [e.raw_content for e in elements] == [_RED, _GREEN, _BLUE]
        assert all(e.element_kind == ElementKind.IMAGE for e in elements)
        assert all(e.metadata.get("whole_page") is True for e in elements)
        assert [e.metadata["image_ext"] for e in elements] == ["png", "jpeg", "png"]

    def test_page_order_follows_the_member_names_not_the_zip_order(
        self, tmp_path: Path
    ) -> None:
        path = _zip(
            tmp_path / "set.zip",
            [("0002.png", _GREEN), ("0003.png", _BLUE), ("0001.png", _RED)],
        )
        assert image_names(path) == ["0001.png", "0002.png", "0003.png"]
        assert [e.raw_content for e in ImageSetExtractor().extract(path)] == [
            _RED,
            _GREEN,
            _BLUE,
        ]

    def test_an_empty_set_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            ImageSetExtractor().extract(_zip(tmp_path / "e.zip", []))

    def test_a_member_that_is_not_an_image_is_refused(self, tmp_path: Path) -> None:
        path = _zip(tmp_path / "s.zip", [("0001.png", _RED), ("0002.txt", b"hello")])
        with pytest.raises(ValueError, match="Not an image"):
            ImageSetExtractor().extract(path)

    def test_a_broken_image_is_refused(self, tmp_path: Path) -> None:
        path = _zip(tmp_path / "s.zip", [("0001.png", b"not really a png")])
        with pytest.raises(ValueError, match="Not a valid image"):
            ImageSetExtractor().extract(path)

    def test_a_file_that_is_not_a_zip_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "s.zip"
        path.write_bytes(b"plain text")
        with pytest.raises(ValueError, match="Not a valid image set"):
            ImageSetExtractor().extract(path)

    def test_zip_files_use_this_extractor(self) -> None:
        assert isinstance(get_extractor(Path("x.zip")), ImageSetExtractor)


class TestWorkerHelpers:
    def test_pages_are_counted_for_the_usage_log(self, tmp_path: Path) -> None:
        assert _count_input_pages(_set(tmp_path)) == 3

    def test_the_request_ceiling_grows_with_the_number_of_images(
        self, tmp_path: Path
    ) -> None:
        small = _set(tmp_path)
        assert _vision_limit(str(small)) == 25  # 3 images: the flat ceiling
        big = _zip(tmp_path / "big.zip", [(f"{i:04d}.png", _RED) for i in range(1, 21)])
        assert _vision_limit(str(big)) == 45  # 20 * 2 + 5


class _Pages(VisionProvider):
    """Answers every page with one text block and remembers what it was shown."""

    def __init__(self) -> None:
        self.images: list[bytes] = []

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        self.images.append(image)
        block = PageBlock(
            kind=BlockKind.TEXT,
            style=TextStyle.PARAGRAPH,
            runs=[TextRun(text=f"Sehife {len(self.images)}")],
            box_2d=[100, 100, 200, 800],
        )
        return VisionResult(
            content_type=ContentType.PAGE, page=PageAnalysis(blocks=[block])
        )


class TestPipeline:
    def test_three_images_make_one_document_of_three_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import docx
        from pptx import Presentation

        fake = _Pages()
        monkeypatch.setattr(vision_pkg, "get_vision_provider", lambda budget: fake)
        path = _set(tmp_path)
        docx_path, pptx_path = _process_job_sync(
            str(path), tmp_path / "out", VisionBudget(limit=25)
        )
        # Each image is read once (the cache serves the second document), unchanged,
        # in the order of the set.
        assert fake.images == [_RED, _GREEN, _BLUE]
        document = docx.Document(docx_path)
        assert len(document.element.body.xpath('.//w:br[@w:type="page"]')) == 2
        texts = [p.text for p in document.paragraphs if p.text]
        assert texts == ["Sehife 1", "Sehife 2", "Sehife 3"]
        assert len(Presentation(pptx_path).slides) == 3


# --------------------------------------------------------------------------
# Database and API (run in CI, where PostgreSQL is available)
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
) -> Iterator[TestClient]:
    job_store, user_store, refresh_token_store = stores
    for email, role in (("admin@test.com", Role.ADMIN), ("a@test.com", Role.MEMBER)):
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


def _login(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/auth/login", json={"email": "a@test.com", "password": "secret-pass"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _merge(
    client: TestClient,
    headers: dict[str, str],
    files: list[tuple[str, bytes, str]],
) -> Any:
    return client.post(
        "/jobs/merge",
        files=[("files", (name, data, mime)) for name, data, mime in files],
        headers=headers,
    )


_THREE = [
    ("sehife1.png", _RED, "image/png"),
    ("sehife2.jpg", _GREEN, "image/jpeg"),
    ("sehife3.png", _BLUE, "image/png"),
]


class TestMergeApiDatabase:
    def test_three_images_become_one_pending_job(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
    ) -> None:
        resp = _merge(client, _login(client), _THREE)
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "pending"
        job = stores[0].get_job(UUID(resp.json()["job_id"]))
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.input_filename == "sehife1 (+2 şəkil)"
        assert job.selected_pages is None
        path = Path(job.input_path)
        assert path.suffix == ".zip"
        try:
            elements = ImageSetExtractor().extract(path)
            # The images are stored unchanged, in the order they were sent.
            assert [e.raw_content for e in elements] == [_RED, _GREEN, _BLUE]
        finally:
            path.unlink(missing_ok=True)

    def test_the_order_sent_is_the_page_order(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
    ) -> None:
        resp = _merge(client, _login(client), list(reversed(_THREE)))
        job = stores[0].get_job(UUID(resp.json()["job_id"]))
        assert job is not None
        try:
            elements = ImageSetExtractor().extract(Path(job.input_path))
            assert [e.raw_content for e in elements] == [_BLUE, _GREEN, _RED]
        finally:
            Path(job.input_path).unlink(missing_ok=True)

    def test_one_image_is_not_a_merge(self, client: TestClient) -> None:
        resp = _merge(client, _login(client), _THREE[:1])
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "merge_needs_two"

    def test_only_images_can_be_merged(self, client: TestClient) -> None:
        files = [*_THREE[:2], ("kitab.pdf", b"%PDF-1.4", "application/pdf")]
        resp = _merge(client, _login(client), files)
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "merge_only_images"

    def test_an_unreadable_image_is_refused_by_name(self, client: TestClient) -> None:
        files = [*_THREE[:2], ("xarab.png", b"not a picture", "image/png")]
        resp = _merge(client, _login(client), files)
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "invalid_image"
        assert "xarab.png" in resp.json()["message"]

    def test_it_counts_as_one_job_for_the_active_job_allowance(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
    ) -> None:
        headers = _login(client)
        for _ in range(3):
            resp = client.post(
                "/jobs", files={"file": ("a.png", _RED, "image/png")}, headers=headers
            )
            assert resp.status_code == 201
        resp = _merge(client, headers, _THREE)
        assert resp.status_code == 429
        assert resp.json()["error_code"] == "too_many_active_jobs"

    def test_a_zip_cannot_be_uploaded_directly(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs",
            files={"file": ("x.zip", b"PK\x03\x04", "application/zip")},
            headers=_login(client),
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "unsupported_format"

    def test_the_download_name_is_the_job_name(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
        tmp_path: Path,
    ) -> None:
        headers = _login(client)
        resp = _merge(client, headers, _THREE)
        job_id = UUID(resp.json()["job_id"])
        docx_path = tmp_path / "output.docx"
        docx_path.write_bytes(b"docx")
        job = stores[0].get_job(job_id)
        assert job is not None
        try:
            stores[0].update_job(
                job_id, status=JobStatus.COMPLETED, output_docx_path=str(docx_path)
            )
            download = client.get(
                f"/jobs/{job_id}/download", params={"format": "docx"}, headers=headers
            )
            assert download.status_code == 200
            disposition = unquote(download.headers["content-disposition"])
            assert "sehife1 (+2 şəkil).docx" in disposition
        finally:
            Path(job.input_path).unlink(missing_ok=True)
