"""PDF extraction using PyMuPDF."""

import logging
from pathlib import Path

import pymupdf

from app.core.ingestion.base import Extractor
from app.models.document import (
    BoundingBox,
    ElementKind,
    ExtractedElement,
)

logger = logging.getLogger(__name__)

_BLOCK_TYPE_TEXT = 0
_BLOCK_TYPE_IMAGE = 1


def count_pdf_pages(data: bytes) -> int | None:
    """Return the page count of PDF ``data``, or ``None`` if it cannot be opened."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return int(doc.page_count)
    except Exception:  # noqa: BLE001 - a broken PDF fails later in the worker
        logger.warning("Could not read the page count of an uploaded PDF")
        return None


class PdfExtractor(Extractor):
    """Extracts text blocks and embedded images from PDF files.

    Uses ``page.get_text("dict")`` so text and image blocks come back in
    the page's natural reading order, each with a bounding box. Text
    blocks that fall inside a detected table region are tagged with
    ``metadata["is_table"] = True`` for the classifier.
    """

    def extract(self, file_path: Path) -> list[ExtractedElement]:
        """Return all text blocks and images of the PDF in reading order."""
        elements: list[ExtractedElement] = []
        with pymupdf.open(file_path) as doc:
            for page_index, page in enumerate(doc):
                page_number = page_index + 1
                table_rects = self._find_table_rects(page)
                for block in page.get_text("dict")["blocks"]:
                    element = self._block_to_element(block, page_number, table_rects)
                    if element is not None:
                        elements.append(element)
        logger.info("Extracted %d elements from PDF %s", len(elements), file_path.name)
        return elements

    @staticmethod
    def _find_table_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
        """Return bounding rectangles of tables detected on the page."""
        try:
            return [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
        except Exception:  # noqa: BLE001 - table detection is best-effort
            logger.warning("Table detection failed on page %d", page.number)
            return []

    @staticmethod
    def _block_to_element(
        block: dict,  # type: ignore[type-arg]
        page_number: int,
        table_rects: list[pymupdf.Rect],
    ) -> ExtractedElement | None:
        """Convert one PyMuPDF block dict into an ExtractedElement."""
        bbox = BoundingBox(
            x0=block["bbox"][0],
            y0=block["bbox"][1],
            x1=block["bbox"][2],
            y1=block["bbox"][3],
        )
        if block["type"] == _BLOCK_TYPE_IMAGE:
            return ExtractedElement(
                source_page=page_number,
                element_kind=ElementKind.IMAGE,
                raw_content=block["image"],
                bounding_box=bbox,
                metadata={"image_ext": block.get("ext", "png")},
            )
        if block["type"] == _BLOCK_TYPE_TEXT:
            text = "\n".join(
                "".join(span["text"] for span in line["spans"])
                for line in block["lines"]
            ).strip()
            if not text:
                return None
            block_rect = pymupdf.Rect(block["bbox"])
            in_table = any(rect.intersects(block_rect) for rect in table_rects)
            return ExtractedElement(
                source_page=page_number,
                element_kind=ElementKind.TEXT_BLOCK,
                raw_content=text,
                bounding_box=bbox,
                metadata={"is_table": in_table},
            )
        return None
