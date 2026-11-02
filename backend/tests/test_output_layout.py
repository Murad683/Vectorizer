"""Layout of the generated DOCX/PPTX (offline: a fake vision provider, no API)."""

from io import BytesIO

import pytest
from PIL import Image
from pptx.util import Inches

from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.rich_text import flatten_title
from app.core.vision.base import VisionProvider
from app.models.document import (
    ContentType,
    ElementKind,
    ExtractedElement,
    VisionResult,
)
from tests.diagram_fixtures import run_fixture

_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_TWO_LINE_TITLE = (
    "119. S=2+4+6+...+n ədədin cəmini hesablamaq üçün\nboşluqların yerinə nə yazılacaq?"
)
_FLAT_TITLE = (
    "119. S=2+4+6+...+n ədədin cəmini hesablamaq üçün boşluqların yerinə nə yazılacaq?"
)


class _Fixed(VisionProvider):
    def __init__(self, result: VisionResult) -> None:
        self.result = result

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        return self.result.model_copy(deep=True)


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _classified(image: bytes):
    return classify_elements(
        [
            ExtractedElement(
                source_page=1,
                element_kind=ElementKind.IMAGE,
                raw_content=image,
                metadata={"image_ext": "png"},
            )
        ]
    )


def _diagram_provider(title: str | None = _TWO_LINE_TITLE) -> _Fixed:
    graph, _ = run_fixture("sum_loop")
    graph.title = title
    graph.title_below = False
    return _Fixed(VisionResult(content_type=ContentType.DIAGRAM, diagram_graph=graph))


def _fallback_provider() -> _Fixed:
    return _Fixed(VisionResult(error="nothing usable"))


def test_flatten_title_joins_lines() -> None:
    assert flatten_title(_TWO_LINE_TITLE) == _FLAT_TITLE
    assert flatten_title("  a \n\n b  ") == "a b"
    assert flatten_title("") == ""


class TestDocx:
    def test_title_is_one_paragraph_without_a_hard_break(self) -> None:
        document = assemble_docx(
            _classified(_png(100, 100)), vision_provider=_diagram_provider()
        )
        assert document.paragraphs[0].text == _FLAT_TITLE
        assert document.element.body.findall(f".//{{{_NS_W}}}br") == []

    def test_figure_starts_below_the_paragraph_top(self) -> None:
        document = assemble_docx(
            _classified(_png(100, 100)), vision_provider=_diagram_provider()
        )
        offsets = document.element.body.findall(
            f".//{{{_NS_WP}}}positionV/{{{_NS_WP}}}posOffset"
        )
        assert [o.text for o in offsets] == ["91440"]

    def test_page_is_a4_with_azerbaijani_language(self) -> None:
        document = assemble_docx(
            _classified(_png(100, 100)), vision_provider=_diagram_provider()
        )
        section = document.sections[0]
        assert abs(section.page_width.mm - 210) < 0.5
        assert abs(section.page_height.mm - 297) < 0.5
        content_width = section.page_width - section.left_margin - section.right_margin
        assert content_width >= Inches(6.0)  # the figure's maximum width fits
        lang = document.styles["Normal"].element.rPr.find(f"{{{_NS_W}}}lang")
        assert lang is not None
        assert lang.get(f"{{{_NS_W}}}val") == "az-Latn-AZ"

    @pytest.mark.parametrize("size", [(740, 494), (1080, 1470), (200, 3000)])
    def test_fallback_picture_fits_the_page(self, size: tuple[int, int]) -> None:
        document = assemble_docx(
            _classified(_png(*size)), vision_provider=_fallback_provider()
        )
        picture = document.inline_shapes[0]
        section = document.sections[0]
        content_width = section.page_width - section.left_margin - section.right_margin
        content_height = (
            section.page_height - section.top_margin - section.bottom_margin
        )
        assert 0 < picture.width <= content_width
        assert 0 < picture.height <= Inches(8.0) + 1
        assert picture.height <= content_height
        assert abs(picture.width / picture.height - size[0] / size[1]) < 0.01


class TestPptx:
    @pytest.mark.parametrize(
        "size", [(740, 494), (1080, 1470), (200, 3000), (3000, 200)]
    )
    def test_fallback_picture_is_inside_the_slide_and_centred(
        self, size: tuple[int, int]
    ) -> None:
        prs = assemble_pptx(
            _classified(_png(*size)), vision_provider=_fallback_provider()
        )
        picture = prs.slides[0].shapes[0]
        assert picture.left >= 0
        assert picture.left + picture.width <= prs.slide_width
        assert picture.top >= 0
        assert picture.top + picture.height <= prs.slide_height
        left_gap = picture.left
        right_gap = prs.slide_width - picture.left - picture.width
        assert abs(left_gap - right_gap) < Inches(0.05)
        assert abs(picture.width / picture.height - size[0] / size[1]) < 0.01

    def test_two_line_title_box_is_flat_and_above_the_diagram(self) -> None:
        long_title = _FLAT_TITLE + " " + _FLAT_TITLE  # needs two lines at 90 chars
        prs = assemble_pptx(
            _classified(_png(100, 100)),
            vision_provider=_diagram_provider(long_title),
        )
        slide = prs.slides[0]
        title = next(
            s for s in slide.shapes if s.has_text_frame and "119." in s.text_frame.text
        )
        assert "\n" not in title.text_frame.text
        assert title.height > Inches(0.34)  # grows with the number of lines
        others = [
            s
            for s in slide.shapes
            if s.shape_id != title.shape_id and s.top is not None
        ]
        assert others
        assert min(s.top for s in others) >= title.top + title.height - Inches(0.01)

    def test_one_line_title_keeps_the_compact_box(self) -> None:
        prs = assemble_pptx(
            _classified(_png(100, 100)),
            vision_provider=_diagram_provider("Sxem 1."),
        )
        title = next(
            s
            for s in prs.slides[0].shapes
            if s.has_text_frame and s.text_frame.text == "Sxem 1."
        )
        assert title.height == Inches(0.28 + 0.06)
