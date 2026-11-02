"""PDF extraction using PyMuPDF.

Every page is first sorted into one of three kinds:

- **scan**: the page is a picture (one image covers most of it, even when a
  hidden OCR text layer sits on top, or it has no text but has an image or
  drawing). It is rendered and handed on as ONE whole-page image, which the
  vision model reads as text + flowchart + figures (page mode).
- **blank**: no text, no image, no drawing. Nothing is produced and no vision
  request is spent.
- **text**: a real text layer. Text and tables are copied directly, embedded
  images keep the one-diagram-per-image behaviour. When the reading order
  cannot be told with confidence (several columns, side notes), the page is
  treated as a scan: one request is cheaper than text in the wrong order.
"""

import logging
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import pymupdf

from app.config import get_settings
from app.core.ingestion.base import Extractor
from app.models.document import (
    BoundingBox,
    ElementKind,
    ExtractedElement,
)

logger = logging.getLogger(__name__)

_BLOCK_TYPE_TEXT = 0
_BLOCK_TYPE_IMAGE = 1

# A block wider than this share of the page spans all columns (a heading).
_WIDE_SHARE = 0.60
# x0 values this close (share of page width) belong to the same column.
_COLUMN_TOLERANCE = 0.04
# Two columns must start at least this far apart (share of page width).
_MIN_COLUMN_GAP = 0.25
# A column needs at least this many blocks to count as a column.
_MIN_COLUMN_BLOCKS = 3
# Longest side of a rendered scan, in pixels (keeps huge sheets reasonable).
_MAX_RENDER_PX = 3000
_JPEG_QUALITY = 85
# Blocks of one stream that go up by more than this (points) are out of order.
_ORDER_SLACK_PT = 3.0
_MAX_DISORDER = 0.05


def count_pdf_pages(data: bytes) -> int | None:
    """Return the page count of PDF ``data``, or ``None`` if it cannot be opened."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return int(doc.page_count)
    except Exception:  # noqa: BLE001 - a broken PDF fails later in the worker
        logger.warning("Could not read the page count of an uploaded PDF")
        return None


Box = tuple[float, float, float, float]


def order_blocks(
    boxes: Sequence[Box], page_width: float, spanning: Collection[int] = ()
) -> list[int] | None:
    """Return the reading order of ``boxes`` as indices, or ``None`` if unsure.

    ``boxes`` are ``(x0, y0, x1, y1)`` in stream order. Blocks in
    ``spanning`` (tables) and blocks wider than 60 % of the page act like
    headings: they split the page into bands. One column keeps the stream
    order when it already runs top to bottom, otherwise it is sorted by
    position; two clear columns are read column after column inside each
    band; anything else (three columns, a side note) is not guessed.
    """
    count = len(boxes)
    if count <= 1:
        return list(range(count))

    def is_wide(i: int) -> bool:
        return i in spanning or boxes[i][2] - boxes[i][0] >= _WIDE_SHARE * page_width

    narrow = sorted(
        (i for i in range(count) if not is_wide(i)), key=lambda i: boxes[i][0]
    )
    columns: list[list[int]] = []
    for i in narrow:
        if (
            columns
            and boxes[i][0] - boxes[columns[-1][0]][0] <= _COLUMN_TOLERANCE * page_width
        ):
            columns[-1].append(i)
        else:
            columns.append([i])
    columns = [c for c in columns if len(c) >= _MIN_COLUMN_BLOCKS]

    if len(columns) <= 1:
        tops = [b[1] for b in boxes]
        jumps = sum(
            1 for a, b in zip(tops, tops[1:], strict=False) if b < a - _ORDER_SLACK_PT
        )
        if jumps <= _MAX_DISORDER * (count - 1):
            return list(range(count))
        return sorted(range(count), key=lambda i: (round(boxes[i][1] / 4), boxes[i][0]))

    if len(columns) == 2:
        left_x = sum(boxes[i][0] for i in columns[0]) / len(columns[0])
        right_x = sum(boxes[i][0] for i in columns[1]) / len(columns[1])
        if right_x - left_x >= _MIN_COLUMN_GAP * page_width:
            boundary = (left_x + right_x) / 2
            ordered: list[int] = []
            band: list[int] = []

            def flush() -> None:
                band.sort(key=lambda i: (boxes[i][0] >= boundary, boxes[i][1]))
                ordered.extend(band)
                band.clear()

            for i in sorted(range(count), key=lambda i: (boxes[i][1], boxes[i][0])):
                if is_wide(i):
                    flush()
                    ordered.append(i)
                else:
                    band.append(i)
            flush()
            return ordered
    return None


class PdfExtractor(Extractor):
    """Extracts text blocks, embedded images and scanned pages from PDF files.

    Uses ``page.get_text("dict")`` so text and image blocks come back with a
    bounding box. Text blocks that fall inside a detected table region are
    tagged with ``metadata["is_table"] = True`` for the classifier. Scanned
    pages come back as one IMAGE element tagged ``whole_page`` (see the module
    docstring for how a page is classified).
    """

    def extract(self, file_path: Path) -> list[ExtractedElement]:
        """Return all elements of the PDF in page order and reading order."""
        elements: list[ExtractedElement] = []
        counts = {"text": 0, "scan": 0, "blank": 0}
        with pymupdf.open(file_path) as doc:
            for page_index, page in enumerate(doc):
                page_number = page_index + 1
                kind = self._page_kind(page)
                page_elements: list[ExtractedElement] | None = None
                if kind == "text":
                    page_elements = self._text_page(page, page_number)
                    if page_elements is None:
                        logger.info(
                            "Page %d: layout not clear, read as scan", page_number
                        )
                        kind = "scan"
                if kind == "scan":
                    page_elements = [self._render_page(page, page_number)]
                if kind == "blank":
                    logger.info("Page %d is blank, skipped", page_number)
                counts[kind] += 1
                elements.extend(page_elements or [])
        logger.info(
            "Extracted %d elements from PDF %s (text pages %d, scans %d, blank %d)",
            len(elements),
            file_path.name,
            counts["text"],
            counts["scan"],
            counts["blank"],
        )
        return elements

    @staticmethod
    def _page_kind(page: pymupdf.Page) -> str:
        """Return ``"scan"``, ``"blank"`` or ``"text"`` for ``page``."""
        settings = get_settings()
        text_chars = len("".join(page.get_text("text").split()))
        page_area = abs(page.rect)
        coverage = 0.0
        images = page.get_image_info()
        for info in images:
            visible = pymupdf.Rect(info["bbox"]) & page.rect
            if page_area > 0:
                coverage = max(coverage, abs(visible) / page_area)
        if coverage >= settings.pdf_scan_coverage:
            return "scan"
        has_drawings = bool(page.get_drawings())
        if text_chars == 0 and not images and not has_drawings:
            return "blank"
        if text_chars < settings.pdf_min_text_chars and (images or has_drawings):
            return "scan"
        return "text"

    def _text_page(
        self, page: pymupdf.Page, page_number: int
    ) -> list[ExtractedElement] | None:
        """Elements of a text-layer page, or ``None`` if the order is unclear."""
        table_rects = self._find_table_rects(page)
        blocks = [
            b
            for b in page.get_text("dict")["blocks"]
            if b["type"] in (_BLOCK_TYPE_TEXT, _BLOCK_TYPE_IMAGE)
        ]
        in_table = {
            i
            for i, b in enumerate(blocks)
            if any(r.intersects(pymupdf.Rect(b["bbox"])) for r in table_rects)
        }
        order = order_blocks(
            [tuple(b["bbox"]) for b in blocks],
            float(page.rect.width),
            spanning=in_table,
        )
        if order is None:
            return None
        elements = []
        for i in order:
            element = self._block_to_element(blocks[i], page_number, table_rects)
            if element is not None:
                elements.append(element)
        return elements

    @staticmethod
    def _render_page(page: pymupdf.Page, page_number: int) -> ExtractedElement:
        """Render a scanned page to one JPEG, marked as a whole page."""
        dpi = get_settings().pdf_scan_dpi
        longest_pt = max(page.rect.width, page.rect.height)
        if longest_pt > 0:
            dpi = max(72, min(dpi, int(_MAX_RENDER_PX / longest_pt * 72)))
        data = page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=_JPEG_QUALITY)
        return ExtractedElement(
            source_page=page_number,
            element_kind=ElementKind.IMAGE,
            raw_content=data,
            bounding_box=BoundingBox(
                x0=page.rect.x0, y0=page.rect.y0, x1=page.rect.x1, y1=page.rect.y1
            ),
            metadata={"image_ext": "jpeg", "whole_page": True},
        )

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
        block: dict[str, Any],
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
