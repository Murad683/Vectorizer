"""Native PPTX shape generation from a layouted diagram.

Every flowchart symbol becomes a real, editable PowerPoint AutoShape
with a real text frame, and every edge becomes a native connector
attached to its shapes (so dragging a shape keeps arrows connected).
This is the client deliverable for diagrams — not an image.
"""

import logging

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.shapes.connector import Connector
from pptx.slide import Slide
from pptx.util import Emu, Pt

from app.models.diagram import LayoutedGraph, LayoutedNode, ShapeType

logger = logging.getLogger(__name__)

SHAPE_TYPE_TO_MSO: dict[ShapeType, MSO_SHAPE] = {
    ShapeType.TERMINATOR: MSO_SHAPE.ROUNDED_RECTANGLE,
    ShapeType.PROCESS: MSO_SHAPE.RECTANGLE,
    ShapeType.DECISION: MSO_SHAPE.DIAMOND,
    ShapeType.INPUT_OUTPUT: MSO_SHAPE.PARALLELOGRAM,
}

# Layout px are treated as CSS px: 1 px = 9525 EMU (96 dpi).
_EMU_PER_PX = 9525

_FONT_SIZE = Pt(12)
_MIN_FONT_SIZE = Pt(8)
# Connection point indices on pptx autoshapes: 0=top, 1=left, 2=bottom, 3=right.
_CXN_TOP, _CXN_LEFT, _CXN_BOTTOM, _CXN_RIGHT = 0, 1, 2, 3


def _px_to_emu(px: float, scale: float) -> Emu:
    """Convert layout px to EMU, applying the fit-to-slide scale."""
    return Emu(int(px * scale * _EMU_PER_PX))


def _font_size_for(node: LayoutedNode, scale: float) -> Pt:
    """Pick a font size that fits the node's text into its (scaled) width."""
    size = int(_FONT_SIZE.pt * scale)
    approx_char_px = 0.66  # width of a char relative to font size, roughly
    while size > int(_MIN_FONT_SIZE.pt):
        if len(node.text) * size * approx_char_px <= node.width * scale:
            break
        size -= 1
    return Pt(max(size, int(_MIN_FONT_SIZE.pt)))


def add_diagram_to_slide(
    graph: LayoutedGraph,
    slide: Slide,
    *,
    offset_x_emu: int = 0,
    offset_y_emu: int = 0,
    max_width_emu: int | None = None,
    max_height_emu: int | None = None,
) -> None:
    """Draw the layouted graph onto ``slide`` as native editable shapes.

    The diagram is uniformly scaled down if it would not fit within
    ``max_width_emu`` × ``max_height_emu`` (never scaled up).
    """
    if not graph.nodes:
        logger.warning("Empty graph; nothing added to slide")
        return

    scale = 1.0
    if max_width_emu is not None:
        scale = min(scale, max_width_emu / (graph.canvas_width * _EMU_PER_PX))
    if max_height_emu is not None:
        scale = min(scale, max_height_emu / (graph.canvas_height * _EMU_PER_PX))

    shapes_by_id = {}
    for node in graph.nodes:
        shape = slide.shapes.add_shape(
            SHAPE_TYPE_TO_MSO[node.shape_type],
            Emu(offset_x_emu + int(node.x * scale * _EMU_PER_PX)),
            Emu(offset_y_emu + int(node.y * scale * _EMU_PER_PX)),
            _px_to_emu(node.width, scale),
            _px_to_emu(node.height, scale),
        )
        # Explicit flat fill/line/text colors: without these the shape
        # inherits the theme's p:style fillRef idx="3" — a GRADIENT fill
        # in the default Office theme, which renders as banding.
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shape.line.color.rgb = RGBColor(0x00, 0x00, 0x00)
        shape.line.width = Pt(1.5)
        shape.shadow.inherit = False
        text_frame = shape.text_frame
        text_frame.word_wrap = True
        text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        paragraph = text_frame.paragraphs[0]
        paragraph.alignment = PP_ALIGN.CENTER
        run = paragraph.add_run()
        run.text = node.text  # real editable text, Azerbaijani preserved
        run.font.size = _font_size_for(node, scale)
        run.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
        shapes_by_id[node.id] = shape

    for edge in graph.edges:
        source = shapes_by_id.get(edge.source_id)
        target = shapes_by_id.get(edge.target_id)
        if source is None or target is None:
            logger.warning(
                "Edge %s->%s references unknown node; skipped",
                edge.source_id,
                edge.target_id,
            )
            continue
        source_node = graph.node_by_id(edge.source_id)
        target_node = graph.node_by_id(edge.target_id)

        if edge.is_back_edge:
            # One bentConnector3 (right → up → left), attached at BOTH
            # ends so Office recomputes the whole path when shapes move.
            # adj1 places the vertical rail as a RATIO of the horizontal
            # span between the two connection points (negative = outside
            # the bounding box, i.e. right of the source), reproducing
            # _rail_x's clearance without any fixed waypoint coordinates.
            connector = slide.shapes.add_connector(
                MSO_CONNECTOR.ELBOW, Emu(0), Emu(0), Emu(0), Emu(0)
            )
            connector.begin_connect(source, _CXN_RIGHT)
            connector.end_connect(target, _CXN_RIGHT)
            _set_connector_adj(
                connector,
                "adj1",
                back_edge_rail_adj(source_node, target_node, _rail_x(graph)),
            )
        elif (
            abs(
                (source_node.x + source_node.width / 2)
                - (target_node.x + target_node.width / 2)
            )
            < 1.0
        ):
            connector = slide.shapes.add_connector(
                MSO_CONNECTOR.STRAIGHT, Emu(0), Emu(0), Emu(0), Emu(0)
            )
            connector.begin_connect(source, _CXN_BOTTOM)
            connector.end_connect(target, _CXN_TOP)
        else:
            # Side branch out of a decision: elbow from the facing side.
            connector = slide.shapes.add_connector(
                MSO_CONNECTOR.ELBOW, Emu(0), Emu(0), Emu(0), Emu(0)
            )
            side = _CXN_LEFT if target_node.x < source_node.x else _CXN_RIGHT
            connector.begin_connect(source, side)
            connector.end_connect(target, _CXN_TOP)
        connector.line.width = Pt(1.5)

        if edge.label:
            _add_edge_label(
                slide,
                edge.label,
                source_node,
                target_node,
                scale,
                offset_x_emu,
                offset_y_emu,
            )

    logger.info(
        "Added %d shapes and %d connectors to slide",
        len(shapes_by_id),
        len(graph.edges),
    )


# Distance (layout px) of the back-edge rail right of the rightmost shape,
# matching svg_debug.py's _BACK_EDGE_OFFSET.
_RAIL_OFFSET_PX = 50.0


def _rail_x(graph: LayoutedGraph) -> float:
    """Layout-px x of the vertical rail back-edges are routed along."""
    return max(n.x + n.width for n in graph.nodes) + _RAIL_OFFSET_PX


def back_edge_rail_adj(
    source_node: LayoutedNode, target_node: LayoutedNode, rail_px: float
) -> int:
    """adj1 value (100000 = 100%) placing a bentConnector3's vertical
    segment on the back-edge rail.

    The ratio is measured along the horizontal span between the two
    connection points (source right-mid → target right-mid), so it stays
    correct when Office rescales the connector after a shape is dragged
    — no absolute coordinates involved. With the target left of the
    source (the normal loop case) the value is negative: the vertical
    segment sits outside the endpoints' bounding box, right of the
    source, exactly where the rail belongs.
    """
    sx = source_node.x + source_node.width
    tx = target_node.x + target_node.width
    span = tx - sx
    if abs(span) < 1.0:
        # Degenerate: both right edges aligned — a ratio cannot express
        # an absolute clearance over a zero span. Fall back to one span
        # of clearance to the right rather than dividing by ~zero.
        return -100000
    return round((rail_px - sx) / span * 100000)


def _set_connector_adj(connector: Connector, name: str, val: int) -> None:
    """Set an explicit adjustment value on a connector's preset geometry."""
    prst_geom = connector._element.spPr.find(qn("a:prstGeom"))
    assert prst_geom is not None  # add_connector always writes a prstGeom
    av_lst = prst_geom.find(qn("a:avLst"))
    if av_lst is None:
        av_lst = prst_geom.makeelement(qn("a:avLst"), {})
        prst_geom.append(av_lst)
    gd = av_lst.makeelement(qn("a:gd"), {"name": name, "fmla": f"val {val}"})
    av_lst.append(gd)


def _add_edge_label(
    slide: Slide,
    label: str,
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    scale: float,
    offset_x_emu: int,
    offset_y_emu: int,
) -> None:
    """Place a small borderless text box near the edge's branch point."""
    sx = source_node.x + source_node.width / 2
    tx = target_node.x + target_node.width / 2
    label_x = (sx + tx) / 2
    label_y = source_node.y + source_node.height / 2 - 24
    box = slide.shapes.add_textbox(
        Emu(offset_x_emu + int(label_x * scale * _EMU_PER_PX)),
        Emu(offset_y_emu + int(label_y * scale * _EMU_PER_PX)),
        _px_to_emu(60, scale),
        _px_to_emu(22, scale),
    )
    frame = box.text_frame
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    run = frame.paragraphs[0].add_run()
    run.text = label
    run.font.size = Pt(max(int(_FONT_SIZE.pt * scale) - 1, int(_MIN_FONT_SIZE.pt)))
    run.font.italic = True
