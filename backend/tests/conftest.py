"""Shared fixtures for the backend test suite."""

from pathlib import Path

import pymupdf
import pytest

from app.models.diagram import DiagramEdge, DiagramGraph, DiagramNode, ShapeType

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


@pytest.fixture(scope="session")
def flowchart_graph() -> DiagramGraph:
    """The real flowchart sample's known structure (question 80).

    8 nodes, one decision with two branches, one back-edge
    (a = a + 16 loops back to the decision).
    """
    return DiagramGraph(
        nodes=[
            DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Başlanğıc"),
            DiagramNode(id="n2", shape_type=ShapeType.INPUT_OUTPUT, text="n"),
            DiagramNode(id="n3", shape_type=ShapeType.PROCESS, text="a = 3"),
            DiagramNode(id="n4", shape_type=ShapeType.DECISION, text="a < 164"),
            DiagramNode(id="n5", shape_type=ShapeType.INPUT_OUTPUT, text="n"),
            DiagramNode(id="n6", shape_type=ShapeType.TERMINATOR, text="Son"),
            DiagramNode(id="n7", shape_type=ShapeType.PROCESS, text="n = n - 8"),
            DiagramNode(id="n8", shape_type=ShapeType.PROCESS, text="a = a + 16"),
        ],
        edges=[
            DiagramEdge(source_id="n1", target_id="n2"),
            DiagramEdge(source_id="n2", target_id="n3"),
            DiagramEdge(source_id="n3", target_id="n4"),
            DiagramEdge(source_id="n4", target_id="n5", label="Yox"),
            DiagramEdge(source_id="n5", target_id="n6"),
            DiagramEdge(source_id="n4", target_id="n7", label="Hə"),
            DiagramEdge(source_id="n7", target_id="n8"),
            DiagramEdge(source_id="n8", target_id="n4"),
        ],
    )


@pytest.fixture(scope="session")
def sample_docx() -> Path:
    """The real Azerbaijani informatics test DOCX from samples/."""
    return SAMPLES_DIR / "informatika_seh_40_41.docx"


@pytest.fixture(scope="session")
def flowchart_png_bytes() -> bytes:
    """Raw bytes of the real flowchart sample image."""
    return (SAMPLES_DIR / "flowchart_a3.png").read_bytes()


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PDF built from the real sample assets.

    No PDF sample was provided in samples/, so this fixture composes one
    from the real flowchart PNG and real question text, using PyMuPDF.
    """
    pdf_path = tmp_path_factory.mktemp("fixtures") / "sample_flowchart.pdf"
    question = (
        "80. Alqoritmin blok-sxeminə əsasən n dəyişəninin çapa veriləcək "
        "qiymətini müəyyən edin."
    )
    answers = "A) 320        B) 147        C) 328        D) 312        E) 171"
    doc = pymupdf.open()
    page = doc.new_page()
    # insert_htmlbox handles the Azerbaijani characters (ə, ı) that the
    # default PDF base fonts cannot encode.
    page.insert_htmlbox(pymupdf.Rect(50, 40, 550, 95), question)
    page.insert_image(
        pymupdf.Rect(150, 100, 450, 400),
        filename=str(SAMPLES_DIR / "flowchart_a3.png"),
    )
    page.insert_htmlbox(pymupdf.Rect(50, 420, 550, 465), answers)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path
