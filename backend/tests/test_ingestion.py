"""Extractor tests against the real sample files."""

from pathlib import Path

import pytest

from app.core.ingestion import get_extractor
from app.core.ingestion.docx_extractor import DocxExtractor
from app.core.ingestion.image_extractor import ImageExtractor
from app.core.ingestion.pdf_extractor import PdfExtractor
from app.models.document import ElementKind


class TestDocxExtractor:
    def test_flowchart_png_extracted_as_image_element(
        self, sample_docx: Path, flowchart_png_bytes: bytes
    ) -> None:
        elements = DocxExtractor().extract(sample_docx)
        images = [e for e in elements if e.element_kind == ElementKind.IMAGE]
        assert len(images) == 2
        assert any(e.raw_content == flowchart_png_bytes for e in images)

    def test_surrounding_text_extracted_as_text_elements(
        self, sample_docx: Path
    ) -> None:
        elements = DocxExtractor().extract(sample_docx)
        texts = [
            str(e.raw_content)
            for e in elements
            if e.element_kind == ElementKind.TEXT_BLOCK
        ]
        assert any("Alqoritmin blok-sxeminə əsasən" in t for t in texts)
        assert any(t.startswith("A) 320") for t in texts)

    def test_flowchart_image_appears_at_its_question_position(
        self, sample_docx: Path, flowchart_png_bytes: bytes
    ) -> None:
        """Reading order is preserved: question 80, then its flowchart."""
        elements = DocxExtractor().extract(sample_docx)
        question_idx = next(
            i
            for i, e in enumerate(elements)
            if e.element_kind == ElementKind.TEXT_BLOCK
            and "80. Alqoritmin blok-sxeminə əsasən" in str(e.raw_content)
        )
        image_idx = next(
            i for i, e in enumerate(elements) if e.raw_content == flowchart_png_bytes
        )
        assert image_idx == question_idx + 1

    def test_tables_tagged_with_is_table(self, sample_docx: Path) -> None:
        elements = DocxExtractor().extract(sample_docx)
        tables = [e for e in elements if e.metadata.get("is_table")]
        assert len(tables) == 3
        assert all(e.element_kind == ElementKind.TEXT_BLOCK for e in tables)
        assert any("Mercedes-Benz" in str(e.raw_content) for e in tables)


class TestPdfExtractor:
    def test_image_and_text_extracted_in_order(self, sample_pdf: Path) -> None:
        elements = PdfExtractor().extract(sample_pdf)
        kinds = [e.element_kind for e in elements]
        assert ElementKind.IMAGE in kinds
        assert ElementKind.TEXT_BLOCK in kinds
        texts = [
            str(e.raw_content)
            for e in elements
            if e.element_kind == ElementKind.TEXT_BLOCK
        ]
        assert any("Alqoritmin blok-sxeminə əsasən" in t for t in texts)
        # Question text comes before the flowchart image on the page.
        first_image_idx = kinds.index(ElementKind.IMAGE)
        assert any(
            "Alqoritmin" in str(e.raw_content) for e in elements[:first_image_idx]
        )

    def test_elements_carry_bounding_boxes_and_page(self, sample_pdf: Path) -> None:
        elements = PdfExtractor().extract(sample_pdf)
        assert elements
        assert all(e.bounding_box is not None for e in elements)
        assert all(e.source_page == 1 for e in elements)


class TestImageExtractor:
    def test_valid_png_extracted_as_single_image_element(
        self, flowchart_png_bytes: bytes, tmp_path: Path
    ) -> None:
        image_path = tmp_path / "photo.png"
        image_path.write_bytes(flowchart_png_bytes)

        elements = ImageExtractor().extract(image_path)

        assert len(elements) == 1
        element = elements[0]
        assert element.element_kind == ElementKind.IMAGE
        assert element.source_page == 1
        assert element.raw_content == flowchart_png_bytes
        assert element.metadata["image_ext"] == "png"

    def test_jpeg_extension_sets_jpeg_image_ext(
        self, flowchart_png_bytes: bytes, tmp_path: Path
    ) -> None:
        # Real bytes don't need to actually be JPEG-encoded for this
        # extractor (Pillow's format sniffing still accepts a PNG payload
        # under a mismatched name) — this only checks the extension →
        # metadata mapping used downstream by the vision call.
        image_path = tmp_path / "photo.jpg"
        image_path.write_bytes(flowchart_png_bytes)

        elements = ImageExtractor().extract(image_path)

        assert elements[0].metadata["image_ext"] == "jpeg"

    def test_corrupt_file_rejected(self, tmp_path: Path) -> None:
        image_path = tmp_path / "broken.png"
        image_path.write_bytes(b"this is not an image, just some bytes")

        with pytest.raises(ValueError, match="Not a valid image file"):
            ImageExtractor().extract(image_path)

    def test_spoofed_extension_rejected(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        """A real DOCX renamed to .png — extension lies about content."""
        image_path = tmp_path / "spoofed.png"
        image_path.write_bytes(sample_docx.read_bytes())

        with pytest.raises(ValueError, match="Not a valid image file"):
            ImageExtractor().extract(image_path)


def test_get_extractor_dispatches_by_extension() -> None:
    assert isinstance(get_extractor(Path("x.pdf")), PdfExtractor)
    assert isinstance(get_extractor(Path("x.DOCX")), DocxExtractor)
    assert isinstance(get_extractor(Path("x.png")), ImageExtractor)
    assert isinstance(get_extractor(Path("x.jpg")), ImageExtractor)
    assert isinstance(get_extractor(Path("x.JPEG")), ImageExtractor)


def test_get_extractor_rejects_unknown_extension() -> None:
    try:
        get_extractor(Path("x.pptx"))
    except ValueError as exc:
        assert "Unsupported file type" in str(exc)
    else:
        raise AssertionError("expected ValueError")
