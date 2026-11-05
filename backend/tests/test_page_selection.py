"""Page selection of scanned PDFs: lists, extractor, assemblers, worker, API."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.cleanup_inputs import expire_selections
from app.config import get_settings
from app.core.assembly import assemble_docx, assemble_pptx
from app.core.auth.models import Role
from app.core.auth.security import hash_password
from app.core.auth.store import RefreshTokenStore, UserStore
from app.core.classifier import classify_elements
from app.core.ingestion import (
    PageSelection,
    classify_pages,
    format_page_list,
    get_extractor,
    parse_page_list,
    render_thumbnails,
)
from app.core.ingestion.pdf_extractor import PdfExtractor
from app.core.queue.models import Job, JobStatus
from app.core.queue.store import JobStore
from app.core.queue.worker import (
    _process_job_sync,
    _selection_of,
    _vision_limit,
    _worker_loop,
)
from app.core.vision import VisionBudget
from app.core.vision.base import VisionProvider
from app.main import create_app
from app.models.document import (
    ElementKind,
    ElementType,
    ExtractedElement,
    VisionResult,
)

_TEXT = "Bu sehifenin metn qati var və kifayət qədər uzundur."


def _png() -> bytes:
    image = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(image).rectangle((50, 50, 550, 300), outline="black", width=5)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf(kinds: str, tmp_path: Path | None = None) -> bytes | Path:
    """A PDF with one page per letter: s = scan, t = text, b = blank."""
    doc = pymupdf.open()
    for letter in kinds:
        page = doc.new_page()
        if letter == "s":
            page.insert_image(page.rect, stream=_png())
        elif letter == "t":
            page.insert_text((72, 100), _TEXT, fontsize=12)
    if tmp_path is None:
        data = bytes(doc.tobytes())
        doc.close()
        return data
    path = tmp_path / "doc.pdf"
    doc.save(str(path))
    doc.close()
    return path


class TestPageLists:
    def test_parse_ranges_and_singles(self) -> None:
        assert parse_page_list("1-3, 7") == [1, 2, 3, 7]
        assert parse_page_list(" 5 ,2,2 ") == [2, 5]
        assert parse_page_list("") == []

    @pytest.mark.parametrize("bad", ["a", "1-", "3-1", "0", "1,,x", "1.5", "-2"])
    def test_garbage_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_page_list(bad)

    def test_format_joins_runs(self) -> None:
        assert format_page_list([7, 1, 2, 3]) == "1-3,7"
        assert format_page_list([4]) == "4"
        assert format_page_list([]) == ""
        assert format_page_list([1, 3, 5, 6]) == "1,3,5-6"

    def test_round_trip(self) -> None:
        pages = [1, 2, 3, 9, 11, 12]
        assert parse_page_list(format_page_list(pages)) == pages


class TestClassifyAndThumbnails:
    def test_kinds_from_bytes_and_from_a_file(self, tmp_path: Path) -> None:
        data = _pdf("tsb")
        assert isinstance(data, bytes)
        kinds = [p.kind for p in classify_pages(data)]
        assert kinds == ["text", "scan", "blank"]
        path = _pdf("tsb", tmp_path)
        assert isinstance(path, Path)
        assert [p.number for p in classify_pages(path)] == [1, 2, 3]

    def test_one_small_jpeg_per_page(self, tmp_path: Path) -> None:
        path = _pdf("tsb", tmp_path)
        assert isinstance(path, Path)
        thumbs = render_thumbnails(path)
        assert len(thumbs) == 3
        assert all(t[:3] == b"\xff\xd8\xff" for t in thumbs)
        assert max(len(t) for t in thumbs) < 30_000


class TestExtractorWithSelection:
    @staticmethod
    def _extract(tmp_path: Path, kinds: str, selection: PageSelection | None) -> Any:
        path = _pdf(kinds, tmp_path)
        assert isinstance(path, Path)
        return PdfExtractor(selection).extract(path)

    def test_no_selection_changes_nothing(self, tmp_path: Path) -> None:
        elements = self._extract(tmp_path, "tsb", None)
        assert [(e.source_page, e.element_kind) for e in elements] == [
            (1, ElementKind.TEXT_BLOCK),
            (2, ElementKind.IMAGE),
        ]
        assert elements[1].metadata.get("whole_page") is True

    def test_unselected_pages_stay_pictures_in_place(self, tmp_path: Path) -> None:
        selection = PageSelection(frozenset({2}), keep_unselected=True)
        elements = self._extract(tmp_path, "tsts", selection)
        assert [(e.source_page, e.metadata) for e in elements] == [
            (1, {"image_ext": "jpeg", "keep_image": True}),
            (2, {"image_ext": "jpeg", "whole_page": True}),
            (3, {"image_ext": "jpeg", "keep_image": True}),
            (4, {"image_ext": "jpeg", "keep_image": True}),
        ]

    def test_unselected_pages_can_be_left_out(self, tmp_path: Path) -> None:
        selection = PageSelection(frozenset({2, 3}), keep_unselected=False)
        elements = self._extract(tmp_path, "tsts", selection)
        assert [e.source_page for e in elements] == [2, 3]
        assert elements[0].metadata.get("whole_page") is True
        assert elements[1].element_kind == ElementKind.TEXT_BLOCK

    def test_an_unselected_blank_page_is_always_left_out(self, tmp_path: Path) -> None:
        selection = PageSelection(frozenset(), keep_unselected=True)
        elements = self._extract(tmp_path, "sbs", selection)
        assert [e.source_page for e in elements] == [1, 3]
        assert all(e.metadata.get("keep_image") for e in elements)

    def test_an_empty_selection_makes_every_page_a_picture(
        self, tmp_path: Path
    ) -> None:
        elements = self._extract(
            tmp_path, "tss", PageSelection(frozenset(), keep_unselected=True)
        )
        assert [e.metadata.get("keep_image") for e in elements] == [True] * 3

    def test_kept_pages_are_rendered_smaller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def width(selection: PageSelection) -> int:
            element = self._extract(tmp_path, "s", selection)[0]
            with Image.open(BytesIO(element.raw_content)) as image:
                return int(image.size[0])

        monkeypatch.setenv("PDF_SCAN_DPI", "150")
        monkeypatch.setenv("PDF_KEPT_PAGE_DPI", "80")
        get_settings.cache_clear()
        read = width(PageSelection(frozenset({1})))
        kept = width(PageSelection(frozenset()))
        assert kept < read

    def test_get_extractor_passes_the_selection_to_pdfs_only(self) -> None:
        selection = PageSelection(frozenset({1}))
        pdf = get_extractor(Path("a.pdf"), selection)
        assert isinstance(pdf, PdfExtractor) and pdf._selection == selection
        assert not isinstance(get_extractor(Path("a.png"), selection), PdfExtractor)


class _NeverAsked(VisionProvider):
    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        raise AssertionError("a kept picture must not reach the vision model")


def _picture(number: int, kept: bool = True) -> ExtractedElement:
    metadata: dict[str, Any] = {"image_ext": "png"}
    metadata["keep_image" if kept else "whole_page"] = True
    return ExtractedElement(
        source_page=number,
        element_kind=ElementKind.IMAGE,
        raw_content=_png(),
        metadata=metadata,
    )


class TestKeptPictures:
    def test_classifier_marks_them_as_pictures(self) -> None:
        classified = classify_elements([_picture(1), _picture(2, kept=False)])
        assert [c.element_type for c in classified] == [
            ElementType.PICTURE,
            ElementType.DIAGRAM,
        ]

    def test_docx_and_pptx_embed_them_without_a_vision_request(self) -> None:
        classified = classify_elements([_picture(1), _picture(2), _picture(3)])
        document = assemble_docx(classified, vision_provider=_NeverAsked())
        assert len(document.inline_shapes) == 3
        assert len(document.element.body.xpath('.//w:br[@w:type="page"]')) == 2
        prs = assemble_pptx(classified, vision_provider=_NeverAsked())
        assert len(prs.slides) == 3


class TestWorkerWithSelection:
    @staticmethod
    def _job(selected: str | None, keep: bool = True) -> Job:
        return Job(
            id=uuid4(),
            input_filename="a.pdf",
            input_path="a.pdf",
            selected_pages=selected,
            keep_unselected=keep,
        )

    def test_selection_of_a_job(self) -> None:
        assert _selection_of(self._job(None)) is None
        assert _selection_of(self._job("1-3,7", keep=False)) == PageSelection(
            frozenset({1, 2, 3, 7}), keep_unselected=False
        )
        assert _selection_of(self._job("")) == PageSelection(frozenset())
        assert _selection_of(self._job("nonsense")) is None

    def test_the_request_ceiling_follows_the_chosen_pages(self, tmp_path: Path) -> None:
        path = _pdf("s" * 40, tmp_path)
        assert isinstance(path, Path)
        assert _vision_limit(str(path)) == 85  # 40 pages
        chosen = PageSelection(frozenset(range(1, 6)))
        assert _vision_limit(str(path), chosen) == 25  # 5 pages: the flat ceiling
        many = PageSelection(frozenset(range(1, 31)))
        assert _vision_limit(str(path), many) == 65
        assert _vision_limit(str(path), PageSelection(frozenset())) == 25

    def test_nothing_chosen_builds_the_whole_document_without_vision(
        self, tmp_path: Path
    ) -> None:
        path = _pdf("sts", tmp_path)
        assert isinstance(path, Path)
        budget = VisionBudget(limit=25)
        docx_path, pptx_path = _process_job_sync(
            str(path), tmp_path / "out", budget, PageSelection(frozenset())
        )
        assert budget.used == 0
        assert Path(docx_path).stat().st_size > 0
        assert Path(pptx_path).stat().st_size > 0


# --------------------------------------------------------------------------
# Database and API (run in CI, where PostgreSQL is available)
# --------------------------------------------------------------------------


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


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
    for email, role in (("admin@test.com", Role.ADMIN), ("a@test.com", Role.MEMBER)):
        user_store.create_user(
            user_id=uuid4(),
            email=email,
            hashed_password=hash_password("secret-pass"),
            role=role,
        )
    user_store.create_user(
        user_id=uuid4(),
        email="b@test.com",
        hashed_password=hash_password("secret-pass"),
        role=Role.MEMBER,
    )
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.job_store = job_store
    app.state.user_store = user_store
    app.state.refresh_token_store = refresh_token_store
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(client: TestClient, email: str = "a@test.com") -> dict[str, str]:
    resp = client.post("/auth/login", json={"email": email, "password": "secret-pass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(client: TestClient, headers: dict[str, str], name: str, data: bytes) -> Any:
    return client.post(
        "/jobs", files={"file": (name, data, "application/pdf")}, headers=headers
    )


def _awaiting(client: TestClient, headers: dict[str, str], kinds: str = "sts") -> str:
    data = _pdf(kinds)
    assert isinstance(data, bytes)
    resp = _upload(client, headers, "scan.pdf", data)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "awaiting_selection"
    return str(resp.json()["job_id"])


class TestSelectionApiDatabase:
    def test_a_pdf_with_scans_waits_for_the_selection(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
    ) -> None:
        headers = _login(client)
        data = _pdf("sts")
        assert isinstance(data, bytes)
        resp = _upload(client, headers, "scan.pdf", data)
        assert resp.status_code == 201
        assert resp.json()["status"] == "awaiting_selection"
        assert resp.json()["page_count"] == 3
        job = stores[0].get_job(resp.json()["job_id"])
        assert job is not None
        assert job.status == JobStatus.AWAITING_SELECTION and job.page_count == 3

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("text.pdf", _pdf("ttt")),
            ("one.pdf", _pdf("s")),
            ("blank.pdf", _pdf("bbb")),
            ("pic.png", _png()),
        ],
    )
    def test_everything_else_starts_at_once(
        self, client: TestClient, name: str, data: bytes
    ) -> None:
        resp = _upload(client, _login(client), name, data)
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"
        assert "page_count" not in resp.json()

    def test_the_switch_turns_the_selection_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, PAGE_SELECTION_ENABLED="false")
        data = _pdf("sts")
        assert isinstance(data, bytes)
        resp = _upload(client, _login(client), "scan.pdf", data)
        assert resp.json()["status"] == "pending"

    def test_a_waiting_job_counts_as_active(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_env(monkeypatch, MAX_ACTIVE_JOBS_PER_USER="1")
        headers = _login(client)
        _awaiting(client, headers)
        data = _pdf("ttt")
        assert isinstance(data, bytes)
        blocked = _upload(client, headers, "text.pdf", data)
        assert blocked.status_code == 429

    def test_pages_endpoint_lists_kind_and_picture(self, client: TestClient) -> None:
        headers = _login(client)
        job_id = _awaiting(client, headers, "tsb")
        resp = client.get(f"/jobs/{job_id}/pages", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_count"] == 3
        assert [(p["n"], p["kind"]) for p in body["pages"]] == [
            (1, "text"),
            (2, "scan"),
            (3, "blank"),
        ]
        assert all(
            p["thumb"].startswith("data:image/jpeg;base64,") for p in body["pages"]
        )

    def test_pages_endpoint_is_private_and_only_while_waiting(
        self, client: TestClient
    ) -> None:
        owner = _login(client)
        job_id = _awaiting(client, owner)
        other = _login(client, "b@test.com")
        assert client.get(f"/jobs/{job_id}/pages", headers=other).status_code == 403
        done = _upload(client, owner, "pic.png", _png()).json()["job_id"]
        resp = client.get(f"/jobs/{done}/pages", headers=owner)
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "job_not_awaiting_selection"

    def test_start_records_the_choice_and_queues_the_job(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
    ) -> None:
        headers = _login(client)
        job_id = _awaiting(client, headers, "ssss")
        resp = client.post(
            f"/jobs/{job_id}/start",
            json={"pages": [3, 1, 2], "keep_unselected": False},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending"
        job = stores[0].get_job(job_id)
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.selected_pages == "1-3" and job.keep_unselected is False
        again = client.post(
            f"/jobs/{job_id}/start", json={"pages": [1]}, headers=headers
        )
        assert again.status_code == 409

    def test_start_validates_the_pages(self, client: TestClient) -> None:
        headers = _login(client)
        job_id = _awaiting(client, headers)
        for pages in ([0], [4], [1, 99]):
            resp = client.post(
                f"/jobs/{job_id}/start", json={"pages": pages}, headers=headers
            )
            assert resp.status_code == 400
            assert resp.json()["error_code"] == "invalid_pages"

    def test_choosing_nothing_needs_a_confirmation(
        self,
        client: TestClient,
        stores: tuple[JobStore, UserStore, RefreshTokenStore],
    ) -> None:
        headers = _login(client)
        job_id = _awaiting(client, headers)
        ask = client.post(f"/jobs/{job_id}/start", json={"pages": []}, headers=headers)
        assert ask.status_code == 409
        assert ask.json()["error_code"] == "selection_empty"
        nothing = client.post(
            f"/jobs/{job_id}/start",
            json={"pages": [], "keep_unselected": False, "confirm_empty": True},
            headers=headers,
        )
        assert nothing.status_code == 400
        assert nothing.json()["error_code"] == "nothing_to_process"
        sure = client.post(
            f"/jobs/{job_id}/start",
            json={"pages": [], "confirm_empty": True},
            headers=headers,
        )
        assert sure.status_code == 200
        job = stores[0].get_job(job_id)
        assert job is not None and job.selected_pages == ""

    def test_start_is_private(self, client: TestClient) -> None:
        job_id = _awaiting(client, _login(client))
        resp = client.post(
            f"/jobs/{job_id}/start",
            json={"pages": [1]},
            headers=_login(client, "b@test.com"),
        )
        assert resp.status_code == 403


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


class TestSelectionInTheStoreDatabase:
    def test_the_worker_hands_the_selection_to_the_pipeline(
        self, job_store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[object] = []

        def pipeline(
            _path: str, _out: Path, _budget: VisionBudget, selection: object = None
        ) -> tuple[str, str]:
            seen.append(selection)
            return "a.docx", "a.pptx"

        monkeypatch.setattr("app.core.queue.worker._process_job_sync", pipeline)
        path = _pdf("sss", tmp_path)
        assert isinstance(path, Path)
        job = job_store.create_job(
            job_id=uuid4(),
            input_filename="a.pdf",
            input_path=str(path),
            status=JobStatus.AWAITING_SELECTION,
            page_count=3,
        )
        assert job_store.start_selected_job(job.id, "2", True) is not None
        _run_worker_until_done(job_store, job.id)
        assert seen == [PageSelection(frozenset({2}), keep_unselected=True)]
        rows = job_store.usage_rows_since(datetime.now(timezone.utc) - timedelta(1))
        assert [r["pages"] for r in rows] == [1]

    def test_start_selected_job_only_works_for_a_waiting_job(
        self, job_store: JobStore, tmp_path: Path
    ) -> None:
        pending = job_store.create_job(
            job_id=uuid4(), input_filename="a.pdf", input_path=str(tmp_path / "a.pdf")
        )
        assert job_store.start_selected_job(pending.id, "1", True) is None

    def test_expired_selections_fail_and_others_stay(
        self, job_store: JobStore, tmp_path: Path
    ) -> None:
        waiting = job_store.create_job(
            job_id=uuid4(),
            input_filename="a.pdf",
            input_path=str(tmp_path / "a.pdf"),
            status=JobStatus.AWAITING_SELECTION,
            page_count=3,
        )
        pending = job_store.create_job(
            job_id=uuid4(), input_filename="b.pdf", input_path=str(tmp_path / "b.pdf")
        )
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        assert (
            expire_selections(job_store, expiry_hours=24, dry_run=True, now=later) == 1
        )
        dry = job_store.get_job(waiting.id)
        assert dry is not None and dry.status == JobStatus.AWAITING_SELECTION
        assert (
            expire_selections(job_store, expiry_hours=24, dry_run=False, now=later) == 1
        )
        failed = job_store.get_job(waiting.id)
        assert failed is not None and failed.status == JobStatus.FAILED
        assert failed.error_message is not None and "seçimi" in failed.error_message
        kept = job_store.get_job(pending.id)
        assert kept is not None and kept.status == JobStatus.PENDING
        # fresh waiting jobs are left alone
        fresh = job_store.create_job(
            job_id=uuid4(),
            input_filename="c.pdf",
            input_path=str(tmp_path / "c.pdf"),
            status=JobStatus.AWAITING_SELECTION,
            page_count=2,
        )
        assert expire_selections(job_store, expiry_hours=24, dry_run=False) == 0
        still = job_store.get_job(fresh.id)
        assert still is not None and still.status == JobStatus.AWAITING_SELECTION
