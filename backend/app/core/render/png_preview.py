"""PNG preview of a layouted diagram (INTERNAL QA ONLY).

Draws exactly the geometry the native renderers receive (node boxes,
``edge_path`` polylines, label boxes) so layout results can be eyeballed
next to the source image without opening Word. Never a client deliverable.
"""

import io
import math

from PIL import Image, ImageDraw, ImageFont

from app.core.rich_text import display_text
from app.core.shape_geometry import document_outline
from app.models.diagram import LayoutedGraph, LayoutedNode, ShapeType

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_LABEL_W, _LABEL_H = 60.0, 22.0


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _outline(node: LayoutedNode) -> list[tuple[float, float]]:
    x, y, w, h = node.x, node.y, node.width, node.height
    if node.shape_type == ShapeType.DECISION:
        return [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]
    if node.shape_type == ShapeType.INPUT_OUTPUT:
        s = h * 0.3
        return [(x + s, y), (x + w, y), (x + w - s, y + h), (x, y + h)]
    if node.shape_type == ShapeType.DOCUMENT:
        return [(x + fx * w, y + fy * h) for fx, fy in document_outline()]
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def render_png(graph: LayoutedGraph, zoom: float = 2.0) -> bytes:
    """Render ``graph`` to PNG bytes at ``zoom`` x layout px."""
    w = int(graph.canvas_width * zoom) + 1
    h = int(graph.canvas_height * zoom) + 1
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    text_font = _font(int(11 * zoom))
    label_font = _font(int(10 * zoom))

    def z(p: tuple[float, float]) -> tuple[float, float]:
        return (p[0] * zoom, p[1] * zoom)

    for node in graph.nodes:
        framed = getattr(node, "framed", True)
        if node.shape_type == ShapeType.TERMINATOR:
            box = [z((node.x, node.y)), z((node.x + node.width, node.y + node.height))]
            draw.rounded_rectangle(box, radius=10 * zoom, outline="black", width=2)
        elif framed and node.shape_type.value != "exit":
            draw.polygon([z(p) for p in _outline(node)], outline="black", width=2)
        cx, cy = node.x + node.width / 2, node.y + node.height / 2
        draw.multiline_text(
            z((cx, cy)),
            display_text(node.text, node.latex),
            fill="black",
            font=text_font,
            anchor="mm",
            align="center",
        )

    for edge in graph.edges:
        path = edge.edge_path or []
        if len(path) < 2:
            continue
        pts = [z((p[0], p[1])) for p in path]
        draw.line(pts, fill="black", width=2)
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        ang = math.atan2(y1 - y0, x1 - x0)
        size = 6 * zoom
        head = [
            (x1, y1),
            (x1 - size * math.cos(ang - 0.4), y1 - size * math.sin(ang - 0.4)),
            (x1 - size * math.cos(ang + 0.4), y1 - size * math.sin(ang + 0.4)),
        ]
        draw.polygon(head, fill="black")
        if edge.label and edge.label_x is not None and edge.label_y is not None:
            lx = edge.label_x + (_LABEL_W if edge.label_align_right else 0)
            draw.text(
                z((lx, edge.label_y)),
                edge.label,
                fill=(0, 0, 160),
                font=label_font,
                anchor="ra" if edge.label_align_right else "la",
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
