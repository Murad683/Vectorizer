"""Shared fixtures for the backend test suite."""

from pathlib import Path

import pymupdf
import pytest

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


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
