"""Native DOCX shape generation from a layouted diagram.

python-docx has no shape API (no equivalent of python-pptx's
``add_shape``), so the diagram is hand-built as WordprocessingML Drawing
XML and injected into ``document.xml``'s body via lxml. python-docx is
used only to create/save the document.

Structure (Phase 4b): the ENTIRE diagram lives in ONE
``w:drawing``/``wp:anchor`` containing a single ``wpc:wpc``
(WordprocessingML Drawing Canvas). Every shape and connector is a
``wps:wsp`` child of that canvas, positioned in the canvas' own
coordinate space (EMU, origin at the canvas top-left). Connectors are
genuinely attached to their shapes via ``a:stCxn``/``a:endCxn``
referencing sibling ``wps:cNvPr`` ids — attachment only works inside a
canvas, which is why the previous per-shape-anchor structure could not
provide it.

The input contract is identical to ``pptx_shapes.add_diagram_to_slide``:
a ``LayoutedGraph`` whose geometry (layout px, 1 px = 9525 EMU) was
computed by the layout engine. The back-edge rail routing math is the
same 3-segment route as the PPTX renderer.
"""

import logging
from xml.sax.saxutils import escape

from docx.document import Document
from lxml import etree

from app.core.render.pptx_shapes import _font_size_for, _rail_x
from app.models.diagram import LayoutedGraph, LayoutedNode, ShapeType

logger = logging.getLogger(__name__)

# OOXML namespaces used by WordprocessingML drawings.
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_NS_WPC = "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
_NSDECL = (
    f'xmlns:w="{_NS_W}" xmlns:wp="{_NS_WP}" xmlns:a="{_NS_A}" '
    f'xmlns:wps="{_NS_WPS}" xmlns:wpc="{_NS_WPC}"'
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
_RAIL_PAD_PX = 20.0  # canvas space kept right of the back-edge rail
# Connection site indices on preset geometries: 0=top, 1=left, 2=bottom,
# 3=right (same convention as the pptx connection point indices).
_CXN_TOP, _CXN_LEFT, _CXN_BOTTOM, _CXN_RIGHT = 0, 1, 2, 3


def _shape_wsp(
    node: LayoutedNode,
    shape_id: int,
    x: int,
    y: int,
    cx: int,
    cy: int,
    font_pt: int,
) -> str:
    """A wps:wsp autoshape with a real, editable centred text box."""
    prst = SHAPE_TYPE_TO_PRST[node.shape_type]
    sz_half_pt = font_pt * 2  # w:sz is in half-points
    return (
        "<wps:wsp>"
        f'<wps:cNvPr id="{shape_id}" name="node-{escape(node.id)}"/>'
        "<wps:cNvSpPr/>"
        "<wps:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
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
    *,
    shape_id: int,
    name: str,
    prst: str,
    x: int,
    y: int,
    cx: int,
    cy: int,
    flip: str,
    start: tuple[int, int] | None,
    end: tuple[int, int] | None,
    arrow: bool = True,
) -> str:
    """A wps:wsp connector, attached via stCxn/endCxn where given.

    ``start``/``end`` are (shape_id, connection_site_idx) of sibling
    shapes inside the same canvas, or None for a free endpoint.
    """
    st = f'<a:stCxn id="{start[0]}" idx="{start[1]}"/>' if start else ""
    en = f'<a:endCxn id="{end[0]}" idx="{end[1]}"/>' if end else ""
    tail = '<a:tailEnd type="triangle"/>' if arrow else ""
    return (
        "<wps:wsp>"
        f'<wps:cNvPr id="{shape_id}" name="{escape(name)}"/>'
        f"<wps:cNvCnPr>{st}{en}</wps:cNvCnPr>"
        "<wps:spPr>"
        f'<a:xfrm{flip}><a:off x="{x}" y="{y}"/>'
        f'<a:ext cx="{max(cx, 1)}" cy="{max(cy, 1)}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
        f'<a:ln w="{_LINE_WIDTH_EMU}">'
        f'<a:solidFill><a:srgbClr val="000000"/></a:solidFill>{tail}</a:ln>'
        "</wps:spPr>"
        "<wps:bodyPr/>"
        "</wps:wsp>"
    )


def _label_wsp(
    label: str, shape_id: int, x: int, y: int, cx: int, cy: int, font_pt: int
) -> str:
    """A borderless, fill-less text box holding an edge label."""
    sz_half_pt = font_pt * 2
    return (
        "<wps:wsp>"
        f'<wps:cNvPr id="{shape_id}" name="label-{shape_id}"/>'
        "<wps:cNvSpPr/>"
        "<wps:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
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
    """Normalise connector endpoints to (x, y, w, h, flip-attrs).

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
    """Inject the layouted graph into ``document`` as ONE native canvas.

    The whole diagram is a single ``wp:anchor`` holding a ``wpc:wpc``
    drawing canvas; shapes/connectors are positioned canvas-relative.

    ``relative_from``:

    - ``"paragraph"`` (default): positionH ``relativeFrom="column"``,
      positionV ``relativeFrom="paragraph"`` — the diagram flows with
      its anchor paragraph. (OOXML's ST_RelFromH has no "paragraph"
      value — that invalid combination is exactly what broke Phase 4's
      output — "column" is the horizontal equivalent.)
    - ``"page"``: both axes ``relativeFrom="page"`` (pinned to the page).

    The diagram is uniformly scaled down (never up) to fit within
    ``max_width_emu`` × ``max_height_emu``.
    """
    if not graph.nodes:
        logger.warning("Empty graph; nothing added to document")
        return
    if relative_from == "paragraph":
        rel_h, rel_v = "column", "paragraph"
    elif relative_from == "page":
        rel_h, rel_v = "page", "page"
    else:
        raise ValueError(f"Unsupported relativeFrom: {relative_from!r}")

    # Canvas size in layout px: the graph bounds plus the back-edge rail.
    rail = _rail_x(graph)
    canvas_w_px = max(graph.canvas_width, rail + _RAIL_PAD_PX)
    canvas_h_px = graph.canvas_height

    scale = 1.0
    if max_width_emu is not None:
        scale = min(scale, max_width_emu / (canvas_w_px * _EMU_PER_PX))
    if max_height_emu is not None:
        scale = min(scale, max_height_emu / (canvas_h_px * _EMU_PER_PX))

    def emu(px: float) -> int:
        return int(px * scale * _EMU_PER_PX)

    next_id = 2  # id 1 is the wp:docPr of the anchor itself
    ids_by_node: dict[str, int] = {}
    children: list[str] = []

    for node in graph.nodes:
        ids_by_node[node.id] = next_id
        children.append(
            _shape_wsp(
                node,
                next_id,
                emu(node.x),
                emu(node.y),
                emu(node.width),
                emu(node.height),
                int(_font_size_for(node, scale).pt),
            )
        )
        next_id += 1

    for edge in graph.edges:
        source = graph.node_by_id(edge.source_id)
        target = graph.node_by_id(edge.target_id)
        source_id = ids_by_node[edge.source_id]
        target_id = ids_by_node[edge.target_id]
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
                last = i == len(segments) - 1
                children.append(
                    _connector_wsp(
                        shape_id=next_id,
                        name=f"backedge-{edge.source_id}-{edge.target_id}-{i}",
                        prst="line",
                        x=emu(x),
                        y=emu(y),
                        cx=emu(w),
                        cy=emu(h),
                        flip=flip,
                        start=(source_id, _CXN_RIGHT) if i == 0 else None,
                        end=(target_id, _CXN_RIGHT) if last else None,
                        # Arrowhead only on the segment entering the target.
                        arrow=last,
                    )
                )
                next_id += 1
        else:
            source_cx = source.x + source.width / 2
            target_cx = target.x + target.width / 2
            if abs(source_cx - target_cx) < 1.0:
                # Same column: straight vertical line, bottom → top.
                x0, y0 = source_cx, source.y + source.height
                x1, y1 = target_cx, target.y
                prst = "line"
                start_idx = _CXN_BOTTOM
            else:
                # Branch out of a decision: leave the facing side at
                # mid-height, one bend, arrive at the target's top.
                y0 = source.y + source.height / 2
                x0 = source.x if target_cx < source_cx else source.x + source.width
                x1, y1 = target_cx, target.y
                prst = "bentConnector2"
                start_idx = _CXN_LEFT if target_cx < source_cx else _CXN_RIGHT
            x, y, w, h, flip = _segment(x0, y0, x1, y1)
            children.append(
                _connector_wsp(
                    shape_id=next_id,
                    name=f"edge-{edge.source_id}-{edge.target_id}",
                    prst=prst,
                    x=emu(x),
                    y=emu(y),
                    cx=emu(w),
                    cy=emu(h),
                    flip=flip,
                    start=(source_id, start_idx),
                    end=(target_id, _CXN_TOP),
                )
            )
            next_id += 1
        if edge.label:
            label_x = (
                (source.x + source.width / 2) + (target.x + target.width / 2)
            ) / 2
            label_y = source.y + source.height / 2 - 24
            children.append(
                _label_wsp(
                    edge.label,
                    next_id,
                    emu(label_x),
                    emu(label_y),
                    emu(60),
                    emu(22),
                    max(int(12 * scale) - 1, 8),
                )
            )
            next_id += 1

    canvas_cx = emu(canvas_w_px)
    canvas_cy = emu(canvas_h_px)
    drawing_xml = (
        f"<w:p {_NSDECL}><w:r><w:drawing>"
        '<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        'relativeHeight="251658240" behindDoc="0" locked="0" '
        'layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="{rel_h}">'
        f"<wp:posOffset>{offset_x_emu}</wp:posOffset></wp:positionH>"
        f'<wp:positionV relativeFrom="{rel_v}">'
        f"<wp:posOffset>{offset_y_emu}</wp:posOffset></wp:positionV>"
        f'<wp:extent cx="{canvas_cx}" cy="{canvas_cy}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        "<wp:wrapNone/>"
        '<wp:docPr id="1" name="diagram-canvas"/>'
        "<wp:cNvGraphicFramePr/>"
        f'<a:graphic><a:graphicData uri="{_NS_WPC}">'
        f"<wpc:wpc>{''.join(children)}</wpc:wpc>"
        "</a:graphicData></a:graphic>"
        "</wp:anchor>"
        "</w:drawing></w:r></w:p>"
    )
    paragraph = etree.fromstring(drawing_xml)
    body = document.element.body
    # Keep the section properties (page setup) as the body's last child.
    sect_pr = body.find(f"{{{_NS_W}}}sectPr")
    if sect_pr is not None:
        sect_pr.addprevious(paragraph)
    else:
        body.append(paragraph)
    logger.info(
        "Injected 1 canvas with %d shapes/connectors into DOCX " "(%d nodes, %d edges)",
        next_id - 2,
        len(graph.nodes),
        len(graph.edges),
    )
