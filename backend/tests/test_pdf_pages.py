"""Scanned PDFs: page types, reading order, request ceiling, page breaks.

Everything is built in memory with PyMuPDF; the vision provider is a fake.
"""

from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from app.config import get_settings
from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.ingestion.pdf_extractor import PdfExtractor, order_blocks
from app.core.queue.worker import _vision_limit
from app.core.vision.base import VisionProvider
from app.models.document import (
    ContentType,
    ElementKind,
    ExtractedElement,
    VisionResult,
)
from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun

_LONG_TEXT = "Bu sehifenin metn qati var və kifayət qədər uzundur."


def _png() -> bytes:
    image = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(image).rectangle((50, 50, 550, 300), outline="black", width=5)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _text_page(doc: pymupdf.Document, text: str = _LONG_TEXT) -> None:
    doc.new_page().insert_text((72, 100), text, fontsize=12)


def _scan_page(doc: pymupdf.Document, ocr_text: str | None = None) -> None:
    page = doc.new_page()
    page.insert_image(page.rect, stream=_png())
    if ocr_text:
        page.insert_text((72, 100), ocr_text, fontsize=12, render_mode=3)


def _save(doc: pymupdf.Document, tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    doc.save(str(path))
    doc.close()
    return path


class TestPageTypes:
    def test_text_page_is_copied_directly(self, tmp_path: Path) -> None:
        doc = pymupdf.open()
        _text_page(doc)
        elements = PdfExtractor().extract(_save(doc, tmp_path))
        assert [e.element_kind for e in elements] == [ElementKind.TEXT_BLOCK]
        assert "metn qati" in str(elements[0].raw_content)
        assert "whole_page" not in elements[0].metadata

    def test_full_page_image_is_one_whole_page_element(self, tmp_path: Path) -> None:
        doc = pymupdf.open()
        _scan_page(doc)
        elements = PdfExtractor().extract(_save(doc, tmp_path))
        assert len(elements) == 1
        element = elements[0]
        assert element.element_kind == ElementKind.IMAGE
        assert element.metadata == {"image_ext": "jpeg", "whole_page": True}
        assert element.source_page == 1
        assert isinstance(element.raw_content, bytes)
        assert element.raw_content[:3] == b"\xff\xd8\xff"  # a JPEG

    def test_a_hidden_ocr_layer_does_not_make_it_a_text_page(
        self, tmp_path: Path
    ) -> None:
        doc = pymupdf.open()
        _scan_page(doc, ocr_text=_LONG_TEXT)
        elements = PdfExtractor().extract(_save(doc, tmp_path))
        assert [e.element_kind for e in elements] == [ElementKind.IMAGE]
        assert elements[0].metadata["whole_page"] is True

    def test_blank_page_produces_nothing(self, tmp_path: Path) -> None:
        doc = pymupdf.open()
        doc.new_page()
        assert PdfExtractor().extract(_save(doc, tmp_path)) == []

    def test_a_page_with_only_a_short_text_and_a_drawing_is_a_scan(
        self, tmp_path: Path
    ) -> None:
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(100, 100, 300, 200))
        page.insert_text((72, 60), "Sxem", fontsize=12)
        elements = PdfExtractor().extract(_save(doc, tmp_path))
        assert [e.metadata.get("whole_page") for e in elements] == [True]

    def test_a_small_picture_beside_text_stays_a_text_page(
        self, sample_pdf: Path
    ) -> None:
        elements = PdfExtractor().extract(sample_pdf)
        assert not any(e.metadata.get("whole_page") for e in elements)
        assert any(e.element_kind == ElementKind.TEXT_BLOCK for e in elements)

    def test_mixed_document_keeps_the_page_order(self, tmp_path: Path) -> None:
        doc = pymupdf.open()
        _text_page(doc, "Birinci sehife " + _LONG_TEXT)
        _scan_page(doc)
        doc.new_page()  # blank
        _text_page(doc, "Dorduncu sehife " + _LONG_TEXT)
        elements = PdfExtractor().extract(_save(doc, tmp_path))
        assert [(e.source_page, e.element_kind) for e in elements] == [
            (1, ElementKind.TEXT_BLOCK),
            (2, ElementKind.IMAGE),
            (4, ElementKind.TEXT_BLOCK),
        ]
        assert elements[1].metadata["whole_page"] is True

    def test_scan_dpi_is_configurable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sizes = []
        for dpi in ("72", "150"):
            monkeypatch.setenv("PDF_SCAN_DPI", dpi)
            get_settings.cache_clear()
            doc = pymupdf.open()
            _scan_page(doc)
            element = PdfExtractor().extract(_save(doc, tmp_path))[0]
            with Image.open(BytesIO(element.raw_content)) as img:  # type: ignore[arg-type]
                sizes.append(img.size[0])
        assert sizes[0] < sizes[1]


def _boxes(
    *cells: tuple[float, float, float, float]
) -> list[tuple[float, float, float, float]]:
    return list(cells)


class TestReadingOrder:
    def test_one_column_in_order_keeps_the_stream(self) -> None:
        boxes = [(50, 50 + 40 * i, 500, 80 + 40 * i) for i in range(5)]
        assert order_blocks(boxes, 595) == [0, 1, 2, 3, 4]

    def test_one_column_out_of_order_is_sorted_by_position(self) -> None:
        boxes = [(50, 50 + 40 * i, 500, 80 + 40 * i) for i in range(5)]
        shuffled = [boxes[3], boxes[0], boxes[4], boxes[1], boxes[2]]
        order = order_blocks(shuffled, 595)
        assert order is not None
        assert [shuffled[i][1] for i in order] == sorted(b[1] for b in boxes)

    def test_two_columns_are_read_column_after_column(self) -> None:
        left = [(50, 100 + 60 * i, 280, 140 + 60 * i) for i in range(4)]
        right = [(320, 100 + 60 * i, 550, 140 + 60 * i) for i in range(4)]
        interleaved = [b for pair in zip(left, right, strict=True) for b in pair]
        order = order_blocks(interleaved, 595)
        assert order is not None
        assert [interleaved[i] for i in order] == left + right

    def test_a_heading_splits_the_page_into_bands(self) -> None:
        heading = (50, 40, 550, 70)
        left = [(50, 100 + 60 * i, 280, 140 + 60 * i) for i in range(3)]
        right = [(320, 100 + 60 * i, 550, 140 + 60 * i) for i in range(3)]
        boxes = [heading, right[0], left[0], right[1], left[1], right[2], left[2]]
        order = order_blocks(boxes, 595)
        assert order is not None
        assert [boxes[i] for i in order] == [heading, *left, *right]

    def test_three_columns_are_not_guessed(self) -> None:
        cols = [
            [
                (40 + 190 * c, 100 + 60 * i, 200 + 190 * c, 140 + 60 * i)
                for i in range(4)
            ]
            for c in range(3)
        ]
        assert order_blocks([b for col in cols for b in col], 595) is None

    def test_a_table_block_acts_like_a_heading(self) -> None:
        boxes = [(50, 50, 280, 90), (320, 50, 550, 90), (50, 100, 550, 300)]
        order = order_blocks(boxes, 595, spanning={2})
        assert order is not None and order[-1] == 2

    def test_unclear_layout_becomes_a_scan(self, tmp_path: Path) -> None:
        doc = pymupdf.open()
        page = doc.new_page()
        for col in range(3):
            for row in range(4):
                page.insert_text(
                    (40 + 190 * col, 100 + 60 * row),
                    f"Sutun {col} setir {row} metni burada",
                    fontsize=9,
                )
        elements = PdfExtractor().extract(_save(doc, tmp_path))
        assert [e.metadata.get("whole_page") for e in elements] == [True]


class TestRequestCeiling:
    @staticmethod
    def _pdf(tmp_path: Path, pages: int) -> Path:
        doc = pymupdf.open()
        for _ in range(pages):
            doc.new_page()
        return _save(doc, tmp_path)

    def test_a_pdf_gets_two_requests_per_page_plus_five(self, tmp_path: Path) -> None:
        assert _vision_limit(str(self._pdf(tmp_path, 50))) == 105

    def test_a_short_pdf_keeps_the_flat_ceiling(self, tmp_path: Path) -> None:
        assert _vision_limit(str(self._pdf(tmp_path, 3))) == 25

    def test_images_and_docx_keep_the_flat_ceiling(self, tmp_path: Path) -> None:
        assert _vision_limit(str(tmp_path / "a.png")) == 25
        assert _vision_limit(str(tmp_path / "a.docx")) == 25

    def test_a_broken_pdf_keeps_the_flat_ceiling(self, tmp_path: Path) -> None:
        broken = tmp_path / "x.pdf"
        broken.write_bytes(b"not a pdf")
        assert _vision_limit(str(broken)) == 25

    def test_the_ceiling_can_be_tuned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VISION_CALLS_PER_PAGE", "3")
        monkeypatch.setenv("VISION_CALLS_EXTRA", "0")
        get_settings.cache_clear()
        assert _vision_limit(str(self._pdf(tmp_path, 20))) == 60


class _PageProvider(VisionProvider):
    """Answers every page with one text line naming the call number."""

    def __init__(self) -> None:
        self.calls = 0

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        self.calls += 1
        block = PageBlock(
            kind=BlockKind.TEXT,
            style=TextStyle.PARAGRAPH,
            runs=[TextRun(text=f"Skan sehife {self.calls}")],
        )
        return VisionResult(
            content_type=ContentType.PAGE, page=PageAnalysis(blocks=[block])
        )


def _elements(*kinds: str) -> list[ExtractedElement]:
    out = []
    for number, kind in enumerate(kinds, start=1):
        if kind == "text":
            out.append(
                ExtractedElement(
                    source_page=number,
                    element_kind=ElementKind.TEXT_BLOCK,
                    raw_content=f"Metn {number}",
                    metadata={"is_table": False},
                )
            )
        else:
            out.append(
                ExtractedElement(
                    source_page=number,
                    element_kind=ElementKind.IMAGE,
                    raw_content=_png(),
                    metadata={"image_ext": "png", "whole_page": True},
                )
            )
    return out


def _page_breaks(document: object) -> int:
    body = document.element.body  # type: ignore[attr-defined]
    return len(body.xpath('.//w:br[@w:type="page"]'))


class TestPageBreaks:
    def test_docx_scans_start_on_their_own_page(self) -> None:
        classified = classify_elements(_elements("text", "scan", "scan", "text"))
        document = assemble_docx(classified, vision_provider=_PageProvider())
        # before scan 1, before scan 2, and before the text that follows scan 2
        assert _page_breaks(document) == 3

    def test_pptx_scans_get_their_own_slides(self) -> None:
        classified = classify_elements(_elements("text", "scan", "scan", "text"))
        prs = assemble_pptx(classified, vision_provider=_PageProvider())
        assert len(prs.slides) == 4

    def test_a_single_scanned_image_adds_no_break(self) -> None:
        classified = classify_elements(_elements("scan"))
        document = assemble_docx(classified, vision_provider=_PageProvider())
        assert _page_breaks(document) == 0
        prs = assemble_pptx(classified, vision_provider=_PageProvider())
        assert len(prs.slides) == 1

    def test_a_text_only_document_flows_as_before(self) -> None:
        classified = classify_elements(_elements("text", "text", "text"))
        document = assemble_docx(classified, vision_provider=_PageProvider())
        assert _page_breaks(document) == 0
        prs = assemble_pptx(classified, vision_provider=_PageProvider())
        assert len(prs.slides) == 1
