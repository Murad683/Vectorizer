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
from pptx.oxml.ns import qn
from pptx.presentation import Presentation as PresentationType
from pptx.slide import Slide
from pptx.text.text import _Paragraph
from pptx.util import Inches, Pt

from app.core.layout import layout_graph
from app.core.render.pptx_shapes import add_diagram_to_slide, set_vertical_align
from app.core.rich_text import markup_runs
from app.core.vision import get_vision_provider
from app.core.vision.base import VisionProvider
from app.models.document import ClassifiedElement, ElementType
from app.models.text_content import TextContent

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
            _add_vision_element(cursor, item, provider, context=last_text)
    logger.info(
        "Assembled PPTX with %d elements on %d slides",
        len(classified),
        len(prs.slides._sldIdLst),
    )
    return prs


def _emit_runs(paragraph: _Paragraph, runs: list[dict[str, str | None]]) -> None:
    """Append run-dicts (``{text, vert_align}``) with subscript baselines.

    Shared by plain text and text-content cells; PowerPoint expresses
    sub/superscript as a ``baseline`` attribute on the run properties.
    """
    for run_data in runs:
        run = paragraph.add_run()
        run.text = run_data["text"] or ""
        run.font.size = _FONT_SIZE
        if run_data.get("vert_align") == "subscript":
            run.font._rPr.set("baseline", "-25000")
        elif run_data.get("vert_align") == "superscript":
            run.font._rPr.set("baseline", "30000")


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
        _emit_runs(paragraph, runs)
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


def _add_vision_element(
    cursor: _SlideCursor,
    item: ClassifiedElement,
    provider: VisionProvider,
    context: str | None,
) -> None:
    """Render a vision-analysed image: text content, diagram, or image.

    TEXT_CONTENT flows inline as a native 2-column table; a DIAGRAM with
    edges gets its own slide of native shapes; anything else falls back
    to embedding the original image on its own slide.
    """
    image = item.element.raw_content
    assert isinstance(image, bytes)
    result = provider.analyze_diagram(image, context=context)

    if result.text_content is not None and _add_text_content(
        cursor, result.text_content
    ):
        return

    graph = result.diagram_graph
    slide = cursor.new_slide()
    if graph is not None and graph.edges:
        diagram_top_in = 0.2
        caption_below = bool(graph.title and graph.title_below)
        if graph.title and not caption_below:
            _add_heading(cursor, graph.title)
            diagram_top_in = cursor.y_in
        # Room for the caption line + gap, still above the slide's text limit.
        reserve_in = 0.6 if caption_below else 0.0
        layouted = layout_graph(graph)
        drawn_h = add_diagram_to_slide(
            layouted,
            slide,
            offset_x_emu=Inches(0.5),
            offset_y_emu=Inches(diagram_top_in),
            max_width_emu=Inches(9),
            max_height_emu=Inches(max(1.0, 7.2 - diagram_top_in - reserve_in)),
        )
        if caption_below and graph.title:
            cursor.y_in = diagram_top_in + drawn_h / 914400 + 0.1
            _add_heading(cursor, graph.title)
    else:
        logger.warning(
            "No usable diagram/text for element %s (%s); embedding original image",
            item.element.id,
            result.error or "degenerate content",
        )
        slide.shapes.add_picture(
            BytesIO(image), Inches(2.5), Inches(0.5), height=Inches(6.0)
        )
    # Content after a diagram/image starts on a fresh slide.
    cursor.slide = None


_NO_STYLE_NO_GRID_GUID = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"


def _remove_pptx_table_borders(table) -> None:  # noqa: ANN001
    """Set each cell's four border lines to noFill so no grid prints.

    Scoped to TEXT_CONTENT tables — a matching question has no visible
    grid in the source scan (students draw connecting lines by hand),
    unlike the real data tables which keep their normal borders.
    """
    for row in table.rows:
        for cell in row.cells:
            tc_pr = cell._tc.get_or_add_tcPr()
            # Insert in reverse so the final order matches the CT_TableCell
            # Properties schema (lnL, lnR, lnT, lnB, …).
            for tag in ("a:lnB", "a:lnT", "a:lnR", "a:lnL"):
                for existing in tc_pr.findall(qn(tag)):
                    tc_pr.remove(existing)
                ln = tc_pr.makeelement(qn(tag), {"w": "0"})
                ln.append(ln.makeelement(qn("a:noFill"), {}))
                tc_pr.insert(0, ln)


def _remove_pptx_table_style(table) -> None:  # noqa: ANN001
    """Strip the colored header/row-banding style layer, not just borders.

    Border lines (``a:lnL``/``a:lnR``/``a:lnT``/``a:lnB`` on each cell)
    are a separate OOXML mechanism from table styling: the coloured
    header row and alternating row bands come from ``a:tableStyleId``
    plus the ``firstRow``/``bandRow`` flags on ``a:tblPr``, which is what
    actually painted q87's table like the real data tables even after
    border lines were zeroed. Point the style at PowerPoint's built-in
    "No Style, No Grid" preset and disable both flags so neither the
    header colour nor the banding activates.
    """
    tbl_pr = table._tbl.find(qn("a:tblPr"))
    tbl_pr.set("firstRow", "0")
    tbl_pr.set("bandRow", "0")
    style_id = tbl_pr.find(qn("a:tableStyleId"))
    style_id.text = _NO_STYLE_NO_GRID_GUID


def _add_heading(cursor: _SlideCursor, heading: str) -> None:
    """Append a plain heading text box directly above a text-content table."""
    height_in = _LINE_H_IN + 0.06
    slide = cursor.fit(height_in)
    box = slide.shapes.add_textbox(
        Inches(_MARGIN_IN),
        Inches(cursor.y_in),
        Inches(_CONTENT_W_IN),
        Inches(height_in),
    )
    frame = box.text_frame
    frame.word_wrap = True
    for chunk, vert in markup_runs(heading):
        run = frame.paragraphs[0].add_run()
        run.text = chunk
        run.font.size = _FONT_SIZE
        set_vertical_align(run, vert)
    cursor.advance(height_in)


def _add_text_content(cursor: _SlideCursor, text_content: TextContent) -> bool:
    """Render a two-list matching question as a plain, unstyled table.

    Flows inline (not on its own slide): it is textual content, so it
    stacks with surrounding text. A heading text box (if present) is
    added directly above the table. No border lines and no table-style
    header/banding (two separate OOXML mechanisms, both cleared). Returns
    False when the content is not a usable two-list structure, so the
    caller can fall back to the image.
    """
    columns = text_content.usable_columns()
    if columns is None:
        return False
    if text_content.heading:
        _add_heading(cursor, text_content.heading)
    left, right = columns
    n_rows = max(len(left.lines), len(right.lines))
    height_in = n_rows * _ROW_H_IN
    slide = cursor.fit(height_in)
    shape = slide.shapes.add_table(
        n_rows,
        2,
        Inches(_MARGIN_IN),
        Inches(cursor.y_in),
        Inches(_CONTENT_W_IN),
        Inches(height_in),
    )
    table = shape.table
    _remove_pptx_table_borders(table)
    _remove_pptx_table_style(table)
    for col_idx, column in enumerate((left, right)):
        for row_idx, line in enumerate(column.lines):
            paragraph = table.cell(row_idx, col_idx).text_frame.paragraphs[0]
            _emit_runs(paragraph, line.to_run_dicts())
    cursor.advance(height_in + 0.1)
    return True
