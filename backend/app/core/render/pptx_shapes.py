"""Native PPTX shape generation from a layouted diagram.

Every flowchart symbol becomes a real, editable PowerPoint AutoShape
with a real text frame, and every edge becomes a native connector
attached to its shapes (so dragging a shape keeps arrows connected).
This is the client deliverable for diagrams — not an image.
"""

import logging

from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.shapes.base import BaseShape
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
        text_frame = shape.text_frame
        text_frame.word_wrap = True
        text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        paragraph = text_frame.paragraphs[0]
        paragraph.alignment = PP_ALIGN.CENTER
        run = paragraph.add_run()
        run.text = node.text  # real editable text, Azerbaijani preserved
        run.font.size = _font_size_for(node, scale)
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
            # PowerPoint's bentConnector auto-routing draws this loop
            # through the middle of the diagram, so the route is built
            # manually as three straight segments on a vertical rail to
            # the right of all shapes — mirroring svg_debug.py's routing.
            _add_back_edge(
                slide,
                source,
                target,
                source_node,
                target_node,
                rail_x=_rail_x(graph),
                scale=scale,
                offset_x_emu=offset_x_emu,
                offset_y_emu=offset_y_emu,
            )
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
            continue
        if (
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


def _add_back_edge(
    slide: Slide,
    source: BaseShape,
    target: BaseShape,
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    *,
    rail_x: float,
    scale: float,
    offset_x_emu: int,
    offset_y_emu: int,
) -> None:
    """Route a loop as three explicit straight segments around the diagram.

    source right-mid → (rail, source y) → (rail, target y) → target
    right-mid. The first segment stays attached to the source shape and
    the last to the target shape; the rail waypoints are fixed
    coordinates, never auto-routed.
    """

    def ex(px: float) -> Emu:
        return Emu(offset_x_emu + int(px * scale * _EMU_PER_PX))

    def ey(px: float) -> Emu:
        return Emu(offset_y_emu + int(px * scale * _EMU_PER_PX))

    sx = source_node.x + source_node.width
    sy = source_node.y + source_node.height / 2
    tx = target_node.x + target_node.width
    ty = target_node.y + target_node.height / 2

    out = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, ex(sx), ey(sy), ex(rail_x), ey(sy)
    )
    out.begin_connect(source, _CXN_RIGHT)
    up = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, ex(rail_x), ey(sy), ex(rail_x), ey(ty)
    )
    back_in = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, ex(rail_x), ey(ty), ex(tx), ey(ty)
    )
    back_in.end_connect(target, _CXN_RIGHT)
    for segment in (out, up, back_in):
        segment.line.width = Pt(1.5)


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
