"""Debug SVG renderer for layouted diagrams.

INTERNAL QA ONLY — this output is a preview artifact for developers and
the future review panel. It is never the client deliverable; clients get
natively editable shapes (PPTX now, DOCX in Phase 4).
"""

import html
import logging

from app.core.shape_geometry import document_outline
from app.models.diagram import DiagramEdge, LayoutedGraph, LayoutedNode, ShapeType

logger = logging.getLogger(__name__)

_FONT_SIZE = 12
_BACK_EDGE_OFFSET = 50.0  # how far right of the columns a loop is routed


def _shape_svg(node: LayoutedNode) -> str:
    """Return the SVG element for the node's flowchart symbol."""
    if node.shape_type == ShapeType.EXIT:
        return ""  # an open arrow end is not drawn
    x, y, w, h = node.x, node.y, node.width, node.height
    style = 'fill="white" stroke="black" stroke-width="1.5"'
    if node.shape_type == ShapeType.TERMINATOR:
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{h / 2}" ry="{h / 2}" {style}/>'
        )
    if node.shape_type == ShapeType.DECISION:
        points = (
            f"{x + w / 2},{y} {x + w},{y + h / 2} "
            f"{x + w / 2},{y + h} {x},{y + h / 2}"
        )
        return f'<polygon points="{points}" {style}/>'
    if node.shape_type == ShapeType.INPUT_OUTPUT:
        skew = h * 0.35
        points = f"{x + skew},{y} {x + w},{y} " f"{x + w - skew},{y + h} {x},{y + h}"
        return f'<polygon points="{points}" {style}/>'
    if node.shape_type == ShapeType.DOCUMENT:
        points = " ".join(f"{x + fx * w},{y + fy * h}" for fx, fy in document_outline())
        return f'<polygon points="{points}" {style}/>'
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" {style}/>'


def _text_svg(node: LayoutedNode) -> str:
    """Return the centred label for a node."""
    cx = node.x + node.width / 2
    lines = node.text.split("\n")
    top = node.y + node.height / 2 - (len(lines) - 1) * _FONT_SIZE * 0.6
    spans = "".join(
        f'<tspan x="{cx}" y="{top + i * _FONT_SIZE * 1.2}">{html.escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    return (
        f'<text text-anchor="middle" dominant-baseline="central" '
        f'font-size="{_FONT_SIZE}" font-family="sans-serif">{spans}</text>'
    )


def _forward_edge_svg(source: LayoutedNode, target: LayoutedNode, label: str) -> str:
    """A straight (or one-elbow) arrow from source bottom to target top."""
    sx = source.x + source.width / 2
    sy = source.y + source.height
    tx = target.x + target.width / 2
    ty = target.y
    if abs(sx - tx) < 1.0:
        path = f"M {sx} {sy} L {tx} {ty}"
    else:
        # Side-by-side branch: leave the decision sideways, then go down.
        sy_mid = source.y + source.height / 2
        sx_edge = source.x if tx < sx else source.x + source.width
        path = f"M {sx_edge} {sy_mid} L {tx} {sy_mid} L {tx} {ty}"
    label_svg = ""
    if label:
        lx = (sx + tx) / 2 + 8
        ly = source.y + source.height / 2 - 6
        label_svg = (
            f'<text x="{lx}" y="{ly}" font-size="{_FONT_SIZE}" '
            f'font-family="sans-serif" font-style="italic">'
            f"{html.escape(label)}</text>"
        )
    return (
        f'<path d="{path}" fill="none" stroke="black" stroke-width="1.5" '
        f'marker-end="url(#arrow)"/>' + label_svg
    )


def _back_edge_svg(
    source: LayoutedNode, target: LayoutedNode, right_x: float, label: str
) -> str:
    """A loop edge routed around the right side of the diagram."""
    sx = source.x + source.width
    sy = source.y + source.height / 2
    tx = target.x + target.width
    ty = target.y + target.height / 2
    rail = right_x + _BACK_EDGE_OFFSET
    path = f"M {sx} {sy} L {rail} {sy} L {rail} {ty} L {tx} {ty}"
    loop_icon = (
        f'<text x="{rail + 4}" y="{(sy + ty) / 2}" font-size="{_FONT_SIZE}" '
        f'font-family="sans-serif">&#8635;</text>'
    )
    label_svg = ""
    if label:
        label_svg = (
            f'<text x="{sx + 8}" y="{sy - 6}" font-size="{_FONT_SIZE}" '
            f'font-family="sans-serif" font-style="italic">'
            f"{html.escape(label)}</text>"
        )
    return (
        f'<path d="{path}" fill="none" stroke="black" stroke-width="1.5" '
        f'stroke-dasharray="none" marker-end="url(#arrow)"/>' + loop_icon + label_svg
    )


def render_svg(graph: LayoutedGraph) -> str:
    """Render the layouted graph as a standalone SVG document string."""
    width = graph.canvas_width + _BACK_EDGE_OFFSET + 20
    height = graph.canvas_height
    right_x = max((n.x + n.width for n in graph.nodes), default=0.0)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>"
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="black"/></marker>'
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="white"/>',
    ]

    def edge_svg(edge: DiagramEdge) -> str:
        source = graph.node_by_id(edge.source_id)
        target = graph.node_by_id(edge.target_id)
        label = edge.label or ""
        if edge.is_back_edge:
            return _back_edge_svg(source, target, right_x, label)
        return _forward_edge_svg(source, target, label)

    parts.extend(edge_svg(edge) for edge in graph.edges)
    for node in graph.nodes:
        parts.append(_shape_svg(node))
        parts.append(_text_svg(node))
    parts.append("</svg>")
    logger.info(
        "Rendered debug SVG: %d nodes, %d edges", len(graph.nodes), len(graph.edges)
    )
    return "\n".join(parts)
