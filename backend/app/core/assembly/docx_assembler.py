"""Assemble a complete Word document from classified elements.

Elements are emitted in SOURCE ORDER: text as paragraphs, tables as
native python-docx tables, diagrams via the existing vision → layout →
``docx_shapes`` chain (unmodified). Styling fidelity is intentionally
limited to reasonable defaults — original fonts/sizes are not
reproduced; subscript/superscript runs captured by the extractor
(``metadata["runs"]``) are preserved.
"""

import logging
from io import BytesIO

import docx
from docx.document import Document
from docx.shared import Emu, Inches

from app.core.layout import layout_graph
from app.core.render.docx_shapes import add_diagram_to_docx
from app.core.vision import get_vision_provider
from app.core.vision.base import VisionProvider
from app.models.document import ClassifiedElement, ElementType

logger = logging.getLogger(__name__)

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

_DIAGRAM_MAX_W = Emu(int(6.0 * 914400))
_DIAGRAM_MAX_H = Emu(int(7.5 * 914400))


def assemble_docx(
    classified: list[ClassifiedElement],
    vision_provider: VisionProvider | None = None,
) -> Document:
    """Build a complete Document from classified elements, in order."""
    document = docx.Document()
    provider: VisionProvider | None = vision_provider
    last_text: str | None = None

    for item in classified:
        if item.element_type == ElementType.TEXT:
            _add_text(document, item)
            last_text = str(item.element.raw_content)
        elif item.element_type == ElementType.TABLE:
            _add_table(document, item)
        elif item.element_type == ElementType.DIAGRAM:
            if provider is None:
                provider = get_vision_provider()
            _add_diagram(document, item, provider, context=last_text)
    logger.info("Assembled DOCX with %d elements", len(classified))
    return document


def _add_text(document: Document, item: ClassifiedElement) -> None:
    """Append a paragraph, re-emitting subscript/superscript runs."""
    runs = item.element.metadata.get("runs")
    paragraph = document.add_paragraph()
    if runs:
        for run_data in runs:
            run = paragraph.add_run(run_data["text"])
            if run_data.get("vert_align") == "subscript":
                run.font.subscript = True
            elif run_data.get("vert_align") == "superscript":
                run.font.superscript = True
    else:
        paragraph.add_run(str(item.element.raw_content))


def _add_table(document: Document, item: ClassifiedElement) -> None:
    """Append the flattened table text as a native Word table."""
    rows = str(item.element.raw_content).split("\n")
    cells_per_row = [row.split("\t") for row in rows]
    n_cols = int(
        item.element.metadata.get("n_cols")
        or max(len(cells) for cells in cells_per_row)
    )
    table = document.add_table(rows=len(cells_per_row), cols=n_cols)
    table.style = "Table Grid"
    for row_idx, cells in enumerate(cells_per_row):
        for col_idx, text in enumerate(cells[:n_cols]):
            table.cell(row_idx, col_idx).text = text


def _add_diagram(
    document: Document,
    item: ClassifiedElement,
    provider: VisionProvider,
    context: str | None,
) -> None:
    """Insert the diagram as native shapes, or the original image on failure."""
    image = item.element.raw_content
    assert isinstance(image, bytes)
    result = provider.analyze_diagram(image, context=context)
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
        document.add_picture(BytesIO(image), width=Inches(4.5))
        return
    layouted = layout_graph(graph)
    add_diagram_to_docx(
        layouted,
        document,
        max_width_emu=_DIAGRAM_MAX_W,
        max_height_emu=_DIAGRAM_MAX_H,
        relative_from="paragraph",
    )
    _flow_text_around(document)


def _flow_text_around(document: Document) -> None:
    """Switch the just-inserted diagram anchor to top-and-bottom wrapping.

    ``docx_shapes`` emits ``wp:wrapNone`` (fine for a standalone
    diagram); in an assembled document the following paragraphs must
    flow BELOW the canvas, not underneath it. Post-processing here keeps
    ``docx_shapes.py`` untouched.
    """
    anchors = document.element.body.findall(f".//{{{_NS_WP}}}anchor")
    anchor = anchors[-1]
    wrap_none = anchor.find(f"{{{_NS_WP}}}wrapNone")
    if wrap_none is not None:
        wrap = anchor.makeelement(f"{{{_NS_WP}}}wrapTopAndBottom", {})
        anchor.replace(wrap_none, wrap)
