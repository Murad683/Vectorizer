"""Assembly tests: full-document DOCX/PPTX from the real sample (vision mocked)."""

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lxml import etree
from pptx.util import Emu

from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.ingestion.docx_extractor import DocxExtractor
from app.models.diagram import DiagramGraph
from app.models.document import ClassifiedElement, ContentType, VisionResult
from app.models.text_content import LabeledList, TextContent, TextLine, TextRun

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": ("http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"),
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
}


@pytest.fixture()
def classified(sample_docx: Path) -> list[ClassifiedElement]:
    return classify_elements(DocxExtractor().extract(sample_docx))


@pytest.fixture()
def mock_vision(flowchart_graph: DiagramGraph, flowchart_png_bytes: bytes) -> MagicMock:
    """Vision that solves the real flowchart and fails on other images."""

    def analyze(image: bytes, context: str | None = None) -> VisionResult:
        if image == flowchart_png_bytes:
            return VisionResult(
                content_type=ContentType.DIAGRAM, diagram_graph=flowchart_graph
            )
        return VisionResult(error="not a flowchart (mock)")

    provider = MagicMock()
    provider.analyze_diagram.side_effect = analyze
    return provider


def _q87_text_content() -> TextContent:
    """The real question-87 matching structure: a 3-line and a 5-line list.

    Each numeral-base value splits into a normal run and a subscript
    base run (e.g. 65 + subscript 10).
    """

    def line(label: str, value: str, base: str) -> TextLine:
        return TextLine(
            runs=[
                TextRun(text=f"{label} {value}"),
                TextRun(text=base, subscript=True),
            ]
        )

    numbered = LabeledList(
        lines=[
            line("1.", "65", "10"),
            line("2.", "51", "10"),
            line("3.", "27", "10"),
        ]
    )
    lettered = LabeledList(
        lines=[
            line("a.", "110011", "2"),
            line("b.", "33", "16"),
            line("c.", "1B", "16"),
            line("d.", "33", "8"),
            line("e.", "101", "8"),
        ]
    )
    return TextContent(lists=[numbered, lettered])


@pytest.fixture()
def mock_vision_with_text(
    flowchart_graph: DiagramGraph,
    flowchart_png_bytes: bytes,
    table87_png_bytes: bytes,
) -> MagicMock:
    """Vision that solves the flowchart AND extracts q87 as text content."""

    def analyze(image: bytes, context: str | None = None) -> VisionResult:
        if image == flowchart_png_bytes:
            return VisionResult(
                content_type=ContentType.DIAGRAM, diagram_graph=flowchart_graph
            )
        if image == table87_png_bytes:
            return VisionResult(
                content_type=ContentType.TEXT_CONTENT,
                text_content=_q87_text_content(),
            )
        return VisionResult(error="unrecognised image (mock)")

    provider = MagicMock()
    provider.analyze_diagram.side_effect = analyze
    return provider


def _parse_package(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                etree.fromstring(zf.read(name))


class TestDocxAssembly:
    def test_full_document_contains_all_element_kinds_in_order(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
        tmp_path: Path,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision)
        path = tmp_path / "full.docx"
        document.save(str(path))
        _parse_package(path)  # every XML part parses

        body = document.element.body
        # Body children preserve source order: the diagram canvas sits
        # right after question 80's paragraph, before the answers.
        children = list(body)
        texts_or_drawings = []
        for child in children:
            if child.tag == f"{{{_NS['w']}}}p":
                if child.findall(f".//{{{_NS['wpc']}}}wpc"):
                    texts_or_drawings.append("<canvas>")
                else:
                    text = "".join(
                        t.text or "" for t in child.findall(f".//{{{_NS['w']}}}t")
                    )
                    texts_or_drawings.append(text)
            elif child.tag == f"{{{_NS['w']}}}tbl":
                texts_or_drawings.append("<table>")
        q80 = next(i for i, t in enumerate(texts_or_drawings) if t.startswith("80."))
        assert texts_or_drawings[q80 + 1] == "<canvas>"
        assert texts_or_drawings[q80 + 2].startswith("A) 320")
        # All three source tables present as native tables.
        assert texts_or_drawings.count("<table>") == 3

    def test_tables_are_native_with_correct_shape(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision)
        tables = document.tables
        assert len(tables) == 3
        marka = tables[0]
        assert marka.cell(0, 1).text == "Marka"
        assert marka.cell(1, 1).text == "Mercedes-Benz"

    def test_subscript_runs_preserved(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision)
        # Question 81's "41x + 36x = 77x" — x's are subscript runs.
        para = next(p for p in document.paragraphs if p.text.startswith("81."))
        sub_runs = [r.text for r in para.runs if r.font.subscript]
        assert sub_runs == ["x", "x", "x"]

    def test_failed_vision_falls_back_to_picture(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision)
        # 2 DIAGRAM elements: flowchart → canvas; table_87 image → mock
        # error → embedded picture fallback.
        canvases = document.element.body.findall(f".//{{{_NS['wpc']}}}wpc")
        assert len(canvases) == 1
        drawings_with_pics = document.element.body.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/picture}pic"
        )
        assert len(drawings_with_pics) == 1

    def test_diagram_anchor_wraps_top_and_bottom(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision)
        anchor = document.element.body.find(f".//{{{_NS['wp']}}}anchor")
        assert anchor.find(f"{{{_NS['wp']}}}wrapNone") is None
        assert anchor.find(f"{{{_NS['wp']}}}wrapTopAndBottom") is not None


class TestPptxAssembly:
    def test_full_presentation_contains_everything(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
        tmp_path: Path,
    ) -> None:
        prs = assemble_pptx(classified, vision_provider=mock_vision)
        path = tmp_path / "full.pptx"
        prs.save(str(path))
        _parse_package(path)  # every XML part parses

        slides = list(prs.slides)
        assert len(slides) > 1  # flow-fill produced multiple slides
        all_text = [
            shape.text_frame.text
            for slide in slides
            for shape in slide.shapes
            if shape.has_text_frame
        ]
        assert any(t.startswith("77.") for t in all_text)  # first question
        assert any("Başlanğıc" in t for t in all_text)  # diagram shapes
        tables = [
            shape for slide in slides for shape in slide.shapes if shape.has_table
        ]
        assert len(tables) == 3
        pictures = [
            shape
            for slide in slides
            for shape in slide.shapes
            if shape.shape_type is not None and "PICTURE" in str(shape.shape_type)
        ]
        assert len(pictures) == 1  # table_87 fallback image

    def test_diagram_gets_its_own_slide_in_sequence(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
    ) -> None:
        prs = assemble_pptx(classified, vision_provider=mock_vision)
        diagram_slides = [
            slide
            for slide in prs.slides
            if any(
                shape.has_text_frame and shape.text_frame.text == "Başlanğıc"
                for shape in slide.shapes
            )
        ]
        assert len(diagram_slides) == 1
        slide = diagram_slides[0]
        # Nothing but the diagram's shapes/connectors/labels on that slide.
        assert all(not shape.has_table for shape in slide.shapes)

    def test_slide_dimensions(
        self,
        classified: list[ClassifiedElement],
        mock_vision: MagicMock,
    ) -> None:
        prs = assemble_pptx(classified, vision_provider=mock_vision)
        assert prs.slide_width == Emu(int(10 * 914400))
        assert prs.slide_height == Emu(int(7.5 * 914400))


class TestTextContentAssembly:
    """Question 87 (a scanned matching question) → editable 2-column list."""

    def test_docx_renders_q87_as_native_two_column_table(
        self,
        classified: list[ClassifiedElement],
        mock_vision_with_text: MagicMock,
        tmp_path: Path,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision_with_text)
        path = tmp_path / "full.docx"
        document.save(str(path))
        _parse_package(path)  # package still valid

        # 3 original native tables + the new q87 2-column matching table.
        assert len(document.tables) == 4
        q87 = next(
            t
            for t in document.tables
            if len(t.columns) == 2
            and t.cell(0, 0).text.startswith("1.")
            and t.cell(0, 1).text.startswith("a.")
        )
        assert len(q87.rows) == 5  # longer (lettered) list; numbered padded
        assert q87.cell(0, 1).text == "a. 1100112"
        assert q87.cell(2, 0).text == "3. 2710"
        # Numbered list has only 3 lines; rows 4-5 left column padded empty.
        assert q87.cell(3, 0).text == ""
        assert q87.cell(4, 0).text == ""
        # No image fallback for q87 (flowchart still a canvas though).
        pics = document.element.body.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/picture}pic"
        )
        assert len(pics) == 0

    def test_docx_q87_subscripts_are_real_subscript_runs(
        self,
        classified: list[ClassifiedElement],
        mock_vision_with_text: MagicMock,
    ) -> None:
        document = assemble_docx(classified, vision_provider=mock_vision_with_text)
        q87 = next(
            t
            for t in document.tables
            if len(t.columns) == 2 and t.cell(0, 0).text.startswith("1.")
        )
        # 65₁₀ → normal "1. 65" + subscript "10".
        cell = q87.cell(0, 0)
        runs = cell.paragraphs[0].runs
        assert [r.text for r in runs] == ["1. 65", "10"]
        assert runs[0].font.subscript is not True
        assert runs[1].font.subscript is True

    def test_pptx_renders_q87_as_native_table_with_subscripts(
        self,
        classified: list[ClassifiedElement],
        mock_vision_with_text: MagicMock,
        tmp_path: Path,
    ) -> None:
        prs = assemble_pptx(classified, vision_provider=mock_vision_with_text)
        path = tmp_path / "full.pptx"
        prs.save(str(path))
        _parse_package(path)

        # Find the 2-column matching table among all pptx tables.
        two_col_tables = [
            shape.table
            for slide in prs.slides
            for shape in slide.shapes
            if shape.has_table and len(shape.table.columns) == 2
        ]
        assert len(two_col_tables) == 1
        q87 = two_col_tables[0]
        assert q87.cell(0, 0).text == "1. 6510"
        assert q87.cell(0, 1).text == "a. 1100112"
        # Subscript run carries a negative baseline in its rPr.
        sub_run = q87.cell(0, 0).text_frame.paragraphs[0].runs[1]
        assert sub_run.text == "10"
        assert sub_run.font._rPr.get("baseline") == "-25000"
        # q87 is NOT an embedded picture.
        pictures = [
            shape
            for slide in prs.slides
            for shape in slide.shapes
            if shape.shape_type is not None and "PICTURE" in str(shape.shape_type)
        ]
        assert len(pictures) == 0

    def test_unusable_text_content_falls_back_to_image(
        self,
        classified: list[ClassifiedElement],
        flowchart_png_bytes: bytes,
        table87_png_bytes: bytes,
        flowchart_graph: DiagramGraph,
    ) -> None:
        """A single-list (non-matching) text_content is not usable → image."""

        def analyze(image: bytes, context: str | None = None) -> VisionResult:
            if image == flowchart_png_bytes:
                return VisionResult(
                    content_type=ContentType.DIAGRAM, diagram_graph=flowchart_graph
                )
            # Only one list → usable_columns() returns None.
            one_list = TextContent(
                lists=[LabeledList(lines=[TextLine(runs=[TextRun(text="1. x")])])]
            )
            return VisionResult(
                content_type=ContentType.TEXT_CONTENT, text_content=one_list
            )

        provider = MagicMock()
        provider.analyze_diagram.side_effect = analyze
        document = assemble_docx(classified, vision_provider=provider)
        # q87 fell back to an embedded image; no extra table added.
        assert len(document.tables) == 3
        pics = document.element.body.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/picture}pic"
        )
        assert len(pics) == 1


def test_subscript_runs_captured_by_extractor(sample_docx: Path) -> None:
    elements = DocxExtractor().extract(sample_docx)
    q81 = next(e for e in elements if str(e.raw_content).startswith("81. 41x"))
    runs = q81.metadata.get("runs")
    assert runs is not None
    subs = [r["text"] for r in runs if r.get("vert_align") == "subscript"]
    assert subs == ["x", "x", "x"]
