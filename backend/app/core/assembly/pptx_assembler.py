"""Assemble a complete PowerPoint presentation from classified elements.

Slide-splitting strategy — "flow-fill, diagrams on their own slide":
the source is a textbook page, not a deck, so there is no natural
one-to-one slide mapping. Text and table elements are stacked
top-to-bottom on the current slide in source order, and a new slide
starts whenever the next element's estimated height no longer fits.
Each DIAGRAM gets a dedicated slide (inserted at its position in the
sequence): the flowcharts are large, and sharing a slide with body text
would force them illegibly small. DOCX inputs carry no page geometry
(``source_page`` is always 1), so estimated heights — not source pages —
drive the pagination.

Styling fidelity is intentionally limited to reasonable defaults;
subscript/superscript runs captured by the extractor are preserved.
"""

import logging
from io import BytesIO
from math import ceil

from pptx import Presentation
from pptx.presentation import Presentation as PresentationType
from pptx.slide import Slide
from pptx.util import Inches, Pt

from app.core.layout import layout_graph
from app.core.render.pptx_shapes import add_diagram_to_slide
from app.core.vision import get_vision_provider
from app.core.vision.base import VisionProvider
from app.models.document import ClassifiedElement, ElementType

logger = logging.getLogger(__name__)

_MARGIN_IN = 0.5
_CONTENT_W_IN = 9.0
_SLIDE_H_IN = 7.5
_BOTTOM_LIMIT_IN = 7.1
_LINE_H_IN = 0.28
_CHARS_PER_LINE = 90
_ROW_H_IN = 0.32
_FONT_SIZE = Pt(12)


class _SlideCursor:
    """Tracks the current slide and vertical fill position."""

    def __init__(self, prs: PresentationType) -> None:
        self._prs = prs
        self._blank_layout = prs.slide_layouts[6]
        self.slide: Slide | None = None
        self.y_in = _MARGIN_IN

    def new_slide(self) -> Slide:
        self.slide = self._prs.slides.add_slide(self._blank_layout)
        self.y_in = _MARGIN_IN
        return self.slide

    def fit(self, height_in: float) -> Slide:
        """Return a slide with room for ``height_in``, advancing pages."""
        if self.slide is None or self.y_in + height_in > _BOTTOM_LIMIT_IN:
            self.new_slide()
        assert self.slide is not None
        return self.slide

    def advance(self, height_in: float) -> None:
        self.y_in += height_in


def assemble_pptx(
    classified: list[ClassifiedElement],
    vision_provider: VisionProvider | None = None,
) -> PresentationType:
    """Build a complete Presentation from classified elements, in order."""
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(_SLIDE_H_IN)
    cursor = _SlideCursor(prs)
    provider: VisionProvider | None = vision_provider
    last_text: str | None = None

    for item in classified:
        if item.element_type == ElementType.TEXT:
            _add_text(cursor, item)
            last_text = str(item.element.raw_content)
        elif item.element_type == ElementType.TABLE:
            _add_table(cursor, item)
        elif item.element_type == ElementType.DIAGRAM:
            if provider is None:
                provider = get_vision_provider()
            _add_diagram(cursor, item, provider, context=last_text)
    logger.info(
        "Assembled PPTX with %d elements on %d slides",
        len(classified),
        len(prs.slides._sldIdLst),
    )
    return prs


def _add_text(cursor: _SlideCursor, item: ClassifiedElement) -> None:
    """Append a text box at the current fill position."""
    text = str(item.element.raw_content)
    lines = max(1, ceil(len(text) / _CHARS_PER_LINE))
    height_in = lines * _LINE_H_IN + 0.06
    slide = cursor.fit(height_in)
    box = slide.shapes.add_textbox(
        Inches(_MARGIN_IN),
        Inches(cursor.y_in),
        Inches(_CONTENT_W_IN),
        Inches(height_in),
    )
    frame = box.text_frame
    frame.word_wrap = True
    paragraph = frame.paragraphs[0]
    runs = item.element.metadata.get("runs")
    if runs:
        for run_data in runs:
            run = paragraph.add_run()
            run.text = run_data["text"]
            run.font.size = _FONT_SIZE
            if run_data.get("vert_align") == "subscript":
                run.font._rPr.set("baseline", "-25000")
            elif run_data.get("vert_align") == "superscript":
                run.font._rPr.set("baseline", "30000")
    else:
        run = paragraph.add_run()
        run.text = text
        run.font.size = _FONT_SIZE
    cursor.advance(height_in)


def _add_table(cursor: _SlideCursor, item: ClassifiedElement) -> None:
    """Append a native pptx table at the current fill position."""
    rows = str(item.element.raw_content).split("\n")
    cells_per_row = [row.split("\t") for row in rows]
    n_cols = int(
        item.element.metadata.get("n_cols")
        or max(len(cells) for cells in cells_per_row)
    )
    height_in = len(cells_per_row) * _ROW_H_IN
    slide = cursor.fit(height_in)
    shape = slide.shapes.add_table(
        len(cells_per_row),
        n_cols,
        Inches(_MARGIN_IN),
        Inches(cursor.y_in),
        Inches(_CONTENT_W_IN),
        Inches(height_in),
    )
    table = shape.table
    for row_idx, cells in enumerate(cells_per_row):
        for col_idx, text in enumerate(cells[:n_cols]):
            cell = table.cell(row_idx, col_idx)
            cell.text = text
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = _FONT_SIZE
    cursor.advance(height_in + 0.1)


def _add_diagram(
    cursor: _SlideCursor,
    item: ClassifiedElement,
    provider: VisionProvider,
    context: str | None,
) -> None:
    """Give the diagram its own slide; fall back to the image on failure."""
    image = item.element.raw_content
    assert isinstance(image, bytes)
    result = provider.analyze_diagram(image, context=context)
    slide = cursor.new_slide()
    graph = result.diagram_graph
    # A "diagram" with no edges is not a flowchart (e.g. a scanned table
    # or list misclassified as DIAGRAM by the image rule) — embed the
    # original image instead of rendering disconnected boxes.
    if result.error or graph is None or not graph.edges:
        logger.warning(
            "No usable diagram for element %s (%s); embedding original image",
            item.element.id,
            result.error or "graph has no edges",
        )
        slide.shapes.add_picture(
            BytesIO(image), Inches(2.5), Inches(0.5), height=Inches(6.0)
        )
    else:
        layouted = layout_graph(graph)
        add_diagram_to_slide(
            layouted,
            slide,
            offset_x_emu=Inches(0.5),
            offset_y_emu=Inches(0.2),
            max_width_emu=Inches(9),
            max_height_emu=Inches(7),
        )
    # Content after a diagram starts on a fresh slide.
    cursor.slide = None
