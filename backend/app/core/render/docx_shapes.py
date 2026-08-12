"""Native DOCX shape generation from a layouted diagram.

python-docx has no shape API (no equivalent of python-pptx's
``add_shape``), so every flowchart symbol and connector is hand-built as
WordprocessingML Drawing XML — a ``wp:anchor`` wrapping a ``wps:wsp``
(Word 2010+ wordprocessingShape) — and injected into ``document.xml``'s
body via lxml. python-docx is used only to create/save the document.

The input contract is identical to ``pptx_shapes.add_diagram_to_slide``:
a ``LayoutedGraph`` whose geometry (layout px, 1 px = 9525 EMU) was
computed by the layout engine. Nothing is re-laid-out here, and the
back-edge rail routing math is ported 1:1 from ``pptx_shapes``.

Anchoring: every shape of a diagram lives in ONE paragraph, each in its
own run, positioned with ``relativeFrom="paragraph"`` (see
``add_diagram_to_docx`` for the rationale).
"""

import logging
from collections.abc import Callable
from xml.sax.saxutils import escape

from docx.document import Document
from lxml import etree

from app.core.render.pptx_shapes import _font_size_for, _rail_x
from app.models.diagram import DiagramEdge, LayoutedGraph, LayoutedNode, ShapeType

logger = logging.getLogger(__name__)

# OOXML namespaces used by WordprocessingML drawings.
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_NSDECL = (
    f'xmlns:w="{_NS_W}" xmlns:wp="{_NS_WP}" ' f'xmlns:a="{_NS_A}" xmlns:wps="{_NS_WPS}"'
)

# DrawingML preset geometries — same four symbols as Phase 3's
# MSO_SHAPE mapping, spelled as prstGeom preset names.
SHAPE_TYPE_TO_PRST: dict[ShapeType, str] = {
    ShapeType.TERMINATOR: "roundRect",
    ShapeType.PROCESS: "rect",
    ShapeType.DECISION: "diamond",
    ShapeType.INPUT_OUTPUT: "parallelogram",
}

_EMU_PER_PX = 9525
_LINE_WIDTH_EMU = 19050  # 1.5 pt, matching the PPTX output
_RAIL_OFFSET_PX = 50.0  # kept equal to pptx_shapes._RAIL_OFFSET_PX


def _emu(px: float, scale: float) -> int:
    return int(px * scale * _EMU_PER_PX)


def _anchor(
    inner: str,
    *,
    x_emu: int,
    y_emu: int,
    cx_emu: int,
    cy_emu: int,
    doc_id: int,
    name: str,
    relative_from: str,
) -> str:
    """Wrap DrawingML in a wp:anchor at an absolute EMU offset."""
    cx_emu = max(cx_emu, 1)
    cy_emu = max(cy_emu, 1)
    return (
        f'<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        f'relativeHeight="{250 + doc_id}" behindDoc="0" locked="0" '
        f'layoutInCell="1" allowOverlap="1">'
        f'<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="{relative_from}">'
        f"<wp:posOffset>{x_emu}</wp:posOffset></wp:positionH>"
        f'<wp:positionV relativeFrom="{relative_from}">'
        f"<wp:posOffset>{y_emu}</wp:posOffset></wp:positionV>"
        f'<wp:extent cx="{cx_emu}" cy="{cy_emu}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f"<wp:wrapNone/>"
        f'<wp:docPr id="{doc_id}" name="{escape(name)}"/>'
        f"<wp:cNvGraphicFramePr/>"
        f'<a:graphic><a:graphicData uri="{_NS_WPS}">{inner}'
        f"</a:graphicData></a:graphic>"
        f"</wp:anchor>"
    )


def _shape_wsp(node: LayoutedNode, cx_emu: int, cy_emu: int, font_pt: int) -> str:
    """A wps:wsp autoshape with a real, editable centred text box."""
    prst = SHAPE_TYPE_TO_PRST[node.shape_type]
    sz_half_pt = font_pt * 2  # w:sz is in half-points
    return (
        "<wps:wsp>"
        "<wps:cNvSpPr/>"
        "<wps:spPr>"
        f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx_emu}" cy="{cy_emu}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
        f'<a:ln w="{_LINE_WIDTH_EMU}">'
        '<a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln>'
        "</wps:spPr>"
        "<wps:txbx><w:txbxContent><w:p>"
        '<w:pPr><w:spacing w:after="0"/><w:jc w:val="center"/></w:pPr>'
        f'<w:r><w:rPr><w:sz w:val="{sz_half_pt}"/><w:szCs w:val="{sz_half_pt}"/>'
        f'</w:rPr><w:t xml:space="preserve">{escape(node.text)}</w:t></w:r>'
        "</w:p></w:txbxContent></wps:txbx>"
        '<wps:bodyPr rot="0" vert="horz" lIns="18000" tIns="18000" '
        'rIns="18000" bIns="18000" anchor="ctr" anchorCtr="0">'
        "<a:noAutofit/></wps:bodyPr>"
        "</wps:wsp>"
    )


def _connector_wsp(
    prst: str, cx_emu: int, cy_emu: int, flip: str, *, arrow: bool = True
) -> str:
    """A wps:wsp connector (line/bentConnector), optionally arrow-tipped."""
    tail = '<a:tailEnd type="triangle"/>' if arrow else ""
    return (
        "<wps:wsp>"
        "<wps:cNvCnPr/>"
        "<wps:spPr>"
        f'<a:xfrm{flip}><a:off x="0" y="0"/>'
        f'<a:ext cx="{max(cx_emu, 1)}" cy="{max(cy_emu, 1)}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
        f'<a:ln w="{_LINE_WIDTH_EMU}">'
        f'<a:solidFill><a:srgbClr val="000000"/></a:solidFill>{tail}</a:ln>'
        "</wps:spPr>"
        "<wps:bodyPr/>"
        "</wps:wsp>"
    )


def _label_wsp(label: str, font_pt: int) -> str:
    """A borderless, fill-less text box holding an edge label."""
    sz_half_pt = font_pt * 2
    return (
        "<wps:wsp>"
        "<wps:cNvSpPr/>"
        "<wps:spPr>"
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "<a:noFill/><a:ln><a:noFill/></a:ln>"
        "</wps:spPr>"
        "<wps:txbx><w:txbxContent><w:p>"
        '<w:pPr><w:spacing w:after="0"/></w:pPr>'
        f'<w:r><w:rPr><w:i/><w:sz w:val="{sz_half_pt}"/>'
        f'<w:szCs w:val="{sz_half_pt}"/></w:rPr>'
        f'<w:t xml:space="preserve">{escape(label)}</w:t></w:r>'
        "</w:p></w:txbxContent></wps:txbx>"
        '<wps:bodyPr lIns="0" tIns="0" rIns="0" bIns="0" anchor="t">'
        "<a:noAutofit/></wps:bodyPr>"
        "</wps:wsp>"
    )


def _segment(
    x0: float, y0: float, x1: float, y1: float
) -> tuple[float, float, float, float, str]:
    """Normalise a connector's endpoints to (x, y, w, h, flip-attrs).

    DrawingML extents must be positive; direction is expressed via
    flipH/flipV on a:xfrm.
    """
    flip = ""
    if x1 < x0:
        flip += ' flipH="1"'
    if y1 < y0:
        flip += ' flipV="1"'
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), flip


def add_diagram_to_docx(
    graph: LayoutedGraph,
    document: Document,
    *,
    offset_x_emu: int = 0,
    offset_y_emu: int = 0,
    max_width_emu: int | None = None,
    max_height_emu: int | None = None,
    relative_from: str = "paragraph",
) -> None:
    """Inject the layouted graph into ``document`` as native Word shapes.

    All drawings share ONE paragraph (one run each) and are anchored
    ``relativeFrom="paragraph"`` by default: every shape is positioned
    against the same anchor paragraph, so if content is later inserted
    above the diagram, Word moves the whole diagram as a unit and the
    shapes cannot drift apart. ``relativeFrom="page"`` (also supported
    via ``relative_from``) pins shapes to fixed page coordinates instead
    — internally just as rigid, but reflowing text then slides under the
    diagram, which is wrong for a document deliverable.

    The diagram is uniformly scaled down (never up) to fit within
    ``max_width_emu`` × ``max_height_emu``.
    """
    if not graph.nodes:
        logger.warning("Empty graph; nothing added to document")
        return
    if relative_from not in ("paragraph", "page"):
        raise ValueError(f"Unsupported relativeFrom: {relative_from!r}")

    scale = 1.0
    if max_width_emu is not None:
        scale = min(scale, max_width_emu / (graph.canvas_width * _EMU_PER_PX))
    if max_height_emu is not None:
        scale = min(scale, max_height_emu / (graph.canvas_height * _EMU_PER_PX))

    doc_id = 1
    runs: list[str] = []

    def add_drawing(
        inner: str, x_px: float, y_px: float, cx_emu: int, cy_emu: int, name: str
    ) -> None:
        nonlocal doc_id
        anchor = _anchor(
            inner,
            x_emu=offset_x_emu + _emu(x_px, scale),
            y_emu=offset_y_emu + _emu(y_px, scale),
            cx_emu=cx_emu,
            cy_emu=cy_emu,
            doc_id=doc_id,
            name=name,
            relative_from=relative_from,
        )
        runs.append(f"<w:r><w:drawing>{anchor}</w:drawing></w:r>")
        doc_id += 1

    for node in graph.nodes:
        cx = _emu(node.width, scale)
        cy = _emu(node.height, scale)
        font_pt = int(_font_size_for(node, scale).pt)
        add_drawing(
            _shape_wsp(node, cx, cy, font_pt),
            node.x,
            node.y,
            cx,
            cy,
            f"node-{node.id}",
        )

    rail = _rail_x(graph)
    for edge in graph.edges:
        source = graph.node_by_id(edge.source_id)
        target = graph.node_by_id(edge.target_id)
        if edge.is_back_edge:
            # Same 3-segment rail route as pptx_shapes._add_back_edge:
            # source right-mid → (rail, sy) → (rail, ty) → target right-mid.
            sx = source.x + source.width
            sy = source.y + source.height / 2
            tx = target.x + target.width
            ty = target.y + target.height / 2
            segments = [
                (sx, sy, rail, sy),
                (rail, sy, rail, ty),
                (rail, ty, tx, ty),
            ]
            for i, (x0, y0, x1, y1) in enumerate(segments):
                x, y, w, h, flip = _segment(x0, y0, x1, y1)
                add_drawing(
                    # Arrowhead only on the final segment entering the target.
                    _connector_wsp(
                        "line",
                        _emu(w, scale),
                        _emu(h, scale),
                        flip,
                        arrow=(i == len(segments) - 1),
                    ),
                    x,
                    y,
                    _emu(w, scale),
                    _emu(h, scale),
                    f"backedge-{edge.source_id}-{edge.target_id}-{i}",
                )
        else:
            _add_forward_edge(edge, source, target, scale, add_drawing)
        if edge.label:
            sx_mid = source.x + source.width / 2
            tx_mid = target.x + target.width / 2
            label_x = (sx_mid + tx_mid) / 2
            label_y = source.y + source.height / 2 - 24
            label_font = max(int(12 * scale) - 1, 8)
            add_drawing(
                _label_wsp(edge.label, label_font),
                label_x,
                label_y,
                _emu(60, scale),
                _emu(22, scale),
                f"label-{edge.source_id}-{edge.target_id}",
            )

    paragraph_xml = f"<w:p {_NSDECL}>{''.join(runs)}</w:p>"
    paragraph = etree.fromstring(paragraph_xml)
    body = document.element.body
    # Keep the section properties (page setup) as the body's last child.
    sect_pr = body.find(f"{{{_NS_W}}}sectPr")
    if sect_pr is not None:
        sect_pr.addprevious(paragraph)
    else:
        body.append(paragraph)
    logger.info(
        "Injected %d native shapes/connectors into DOCX (%d nodes, %d edges)",
        doc_id - 1,
        len(graph.nodes),
        len(graph.edges),
    )


def _add_forward_edge(
    edge: DiagramEdge,
    source: LayoutedNode,
    target: LayoutedNode,
    scale: float,
    add_drawing: Callable[[str, float, float, int, int, str], None],
) -> None:
    """Port of pptx_shapes' forward-edge routing to DrawingML presets."""
    source_cx = source.x + source.width / 2
    target_cx = target.x + target.width / 2
    if abs(source_cx - target_cx) < 1.0:
        # Same column: straight vertical line, bottom → top.
        x0, y0 = source_cx, source.y + source.height
        x1, y1 = target_cx, target.y
        prst = "line"
    else:
        # Side branch out of a decision: leave the facing side at
        # mid-height, one bend, arrive at the target's top.
        y0 = source.y + source.height / 2
        x0 = source.x if target_cx < source_cx else source.x + source.width
        x1, y1 = target_cx, target.y
        prst = "bentConnector2"
    x, y, w, h, flip = _segment(x0, y0, x1, y1)
    add_drawing(
        _connector_wsp(prst, _emu(w, scale), _emu(h, scale), flip),
        x,
        y,
        _emu(w, scale),
        _emu(h, scale),
        f"edge-{edge.source_id}-{edge.target_id}",
    )
