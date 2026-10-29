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

from app.core.render.pptx_shapes import (
    _font_size_for,
    assert_no_diagonal_connector,
    choose_connector_from_path,
    parallelogram_geometry_xml,
)
from app.core.rich_text import M_NS, LatexError, latex_to_omml, markup_runs
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
    f'xmlns:wps="{_NS_WPS}" xmlns:wpc="{_NS_WPC}" xmlns:m="{M_NS}"'
)

SHAPE_TYPE_TO_PRST: dict[ShapeType, str] = {
    ShapeType.TERMINATOR: "roundRect",
    ShapeType.PROCESS: "rect",
    ShapeType.DECISION: "diamond",
    ShapeType.INPUT_OUTPUT: "parallelogram",
    ShapeType.DOCUMENT: "flowChartDocument",
    ShapeType.PREPARATION: "flowChartPreparation",
}

_EMU_PER_PX = 9525
_LINE_WIDTH_EMU = 19050  # 1.5 pt
_CXN_TOP, _CXN_LEFT, _CXN_BOTTOM, _CXN_RIGHT = 0, 1, 2, 3

_LABEL_WIDTH_PX = 60.0
_LABEL_HEIGHT_PX = 22.0


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
    paragraphs = _text_paragraphs(node.text, node.latex, font_pt * 2)
    if node.shape_type == ShapeType.INPUT_OUTPUT and node.framed:
        geometry = parallelogram_geometry_xml(cx, cy)
    else:
        prst = SHAPE_TYPE_TO_PRST[node.shape_type] if node.framed else "rect"
        geometry = f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
    if node.framed:
        paint = (
            '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
            f'<a:ln w="{_LINE_WIDTH_EMU}">'
            '<a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln>'
        )
    else:  # drawn without an outline in the source: text only
        paint = "<a:noFill/><a:ln><a:noFill/></a:ln>"
    # Without a frame no inset is needed: give a bare formula the full width.
    ins = 18000 if node.framed else 0
    return (
        "<wps:wsp>"
        f'<wps:cNvPr id="{shape_id}" name="node-{escape(node.id)}"/>'
        "<wps:cNvSpPr/>"
        "<wps:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        f"{geometry}{paint}"
        "</wps:spPr>"
        f"<wps:txbx><w:txbxContent>{paragraphs}</w:txbxContent></wps:txbx>"
        f'<wps:bodyPr rot="0" vert="horz" lIns="{ins}" tIns="{ins}" '
        f'rIns="{ins}" bIns="{ins}" anchor="ctr" anchorCtr="0">'
        "<a:noAutofit/></wps:bodyPr>"
        "</wps:wsp>"
    )


def _text_runs(text: str, half_points: int, italic: bool = False) -> str:
    """WordprocessingML runs for one line, honouring _{...}/^{...} markup."""
    out = []
    for chunk, vert in markup_runs(text):
        va = {"sub": "subscript", "sup": "superscript"}.get(vert or "")
        props = (
            ("<w:i/>" if italic else "")
            + (f'<w:vertAlign w:val="{va}"/>' if va else "")
            + f'<w:sz w:val="{half_points}"/><w:szCs w:val="{half_points}"/>'
        )
        out.append(
            f"<w:r><w:rPr>{props}</w:rPr>"
            f'<w:t xml:space="preserve">{escape(chunk)}</w:t></w:r>'
        )
    return "".join(out) or (
        f'<w:r><w:rPr><w:sz w:val="{half_points}"/></w:rPr><w:t></w:t></w:r>'
    )


def _text_paragraphs(text: str, latex: str | None, half_points: int) -> str:
    """Centred paragraphs: a real Word equation for a formula node, else one
    paragraph per text line with sub/superscript runs."""
    ppr = '<w:pPr><w:spacing w:after="0"/><w:jc w:val="center"/></w:pPr>'
    if latex:
        try:
            return f"<w:p>{ppr}{latex_to_omml(latex, half_points)}</w:p>"
        except LatexError:
            logger.warning("Unsupported formula %r; using its text", latex)
    return "".join(
        f"<w:p>{ppr}{_text_runs(line, half_points)}</w:p>" for line in text.split("\n")
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
    start: tuple[int, int],
    end: tuple[int, int],
    arrow: bool = True,
    adj: dict[str, int] | None = None,
) -> str:
    """A wps:wsp connector, attached at both ends via stCxn/endCxn."""
    st = f'<a:stCxn id="{start[0]}" idx="{start[1]}"/>'
    en = f'<a:endCxn id="{end[0]}" idx="{end[1]}"/>'
    tail = '<a:tailEnd type="triangle"/>' if arrow else ""
    av = "".join(f'<a:gd name="{n}" fmla="val {v}"/>' for n, v in (adj or {}).items())
    return (
        "<wps:wsp>"
        f'<wps:cNvPr id="{shape_id}" name="{escape(name)}"/>'
        f"<wps:cNvCnPr>{st}{en}</wps:cNvCnPr>"
        "<wps:spPr>"
        f'<a:xfrm{flip}><a:off x="{x}" y="{y}"/>'
        f'<a:ext cx="{max(cx, 1)}" cy="{max(cy, 1)}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst>{av}</a:avLst></a:prstGeom>'
        f'<a:ln w="{_LINE_WIDTH_EMU}">'
        f'<a:solidFill><a:srgbClr val="000000"/></a:solidFill>{tail}</a:ln>'
        "</wps:spPr>"
        "<wps:bodyPr/>"
        "</wps:wsp>"
    )


def _freeform_wsp(shape_id: int, name: str, pts: list[tuple[int, int]]) -> str:
    """An open, fill-less polyline (custom geometry) with an arrowhead."""
    min_x = min(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    w = max(max(p[0] for p in pts) - min_x, 1)
    h = max(max(p[1] for p in pts) - min_y, 1)
    rel = [(p[0] - min_x, p[1] - min_y) for p in pts]
    path = f'<a:moveTo><a:pt x="{rel[0][0]}" y="{rel[0][1]}"/></a:moveTo>' + "".join(
        f'<a:lnTo><a:pt x="{x}" y="{y}"/></a:lnTo>' for x, y in rel[1:]
    )
    return (
        "<wps:wsp>"
        f'<wps:cNvPr id="{shape_id}" name="{escape(name)}"/>'
        "<wps:cNvSpPr/>"
        "<wps:spPr>"
        f'<a:xfrm><a:off x="{min_x}" y="{min_y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        "<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/>"
        '<a:rect l="0" t="0" r="r" b="b"/>'
        f'<a:pathLst><a:path w="{w}" h="{h}" fill="none">{path}</a:path></a:pathLst>'
        "</a:custGeom><a:noFill/>"
        f'<a:ln w="{_LINE_WIDTH_EMU}">'
        '<a:solidFill><a:srgbClr val="000000"/></a:solidFill>'
        '<a:tailEnd type="triangle"/></a:ln>'
        "</wps:spPr>"
        "<wps:bodyPr/>"
        "</wps:wsp>"
    )


def _label_wsp(
    label: str,
    shape_id: int,
    x: int,
    y: int,
    cx: int,
    cy: int,
    font_pt: int,
    align_right: bool = False,
) -> str:
    """A borderless, fill-less text box holding an edge label."""
    sz_half_pt = font_pt * 2
    jc = '<w:jc w:val="right"/>' if align_right else ""
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
        f'<w:pPr><w:spacing w:after="0"/>{jc}</w:pPr>'
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
    """Normalise connector endpoints to (x, y, w, h, flip-attrs)."""
    flip = ""
    if x1 < x0:
        flip += ' flipH="1"'
    if y1 < y0:
        flip += ' flipV="1"'
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), flip


def _cxn_site_pos_docx(node: LayoutedNode, idx: int) -> tuple[float, float]:
    """Return the (x, y) position of a connection site on a node."""
    cx = node.x + node.width / 2
    cy = node.y + node.height / 2
    if idx == _CXN_TOP:
        return cx, node.y
    elif idx == _CXN_BOTTOM:
        return cx, node.y + node.height
    elif idx == _CXN_LEFT:
        return node.x, cy
    else:
        return node.x + node.width, cy


def _nearest_site_docx(node: LayoutedNode, pt: list[float]) -> int:
    """Index of the connection site closest to ``pt``."""
    return min(
        (_CXN_TOP, _CXN_LEFT, _CXN_BOTTOM, _CXN_RIGHT),
        key=lambda i: (
            (_cxn_site_pos_docx(node, i)[0] - pt[0]) ** 2
            + (_cxn_site_pos_docx(node, i)[1] - pt[1]) ** 2
        ),
    )


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
    """Inject the layouted graph into ``document`` as ONE native canvas."""
    if not graph.nodes:
        logger.warning("Empty graph; nothing added to document")
        return
    if relative_from == "paragraph":
        rel_h, rel_v = "column", "paragraph"
    elif relative_from == "page":
        rel_h, rel_v = "page", "page"
    else:
        raise ValueError(f"Unsupported relativeFrom: {relative_from!r}")

    canvas_w_px = graph.canvas_width
    canvas_h_px = graph.canvas_height

    scale = 1.0
    if max_width_emu is not None:
        scale = min(scale, max_width_emu / (canvas_w_px * _EMU_PER_PX))
    if max_height_emu is not None:
        scale = min(scale, max_height_emu / (canvas_h_px * _EMU_PER_PX))

    def emu(px: float) -> int:
        return int(px * scale * _EMU_PER_PX)

    next_id = 2
    ids_by_node: dict[str, int] = {}
    children: list[str] = []

    for node in graph.nodes:
        if node.shape_type == ShapeType.EXIT:
            continue  # an open arrow end is not drawn
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

    # Precompute every node's and label's layout-px box once, so a
    # connector that needs a bend position without real Graphviz path
    # data (see _enforce_min_bends_for_axis) can steer clear of them
    # instead of blindly bisecting onto a label.
    node_boxes = {n.id: (n.x, n.y, n.x + n.width, n.y + n.height) for n in graph.nodes}
    label_boxes = [
        (
            e.label_x,
            e.label_y,
            e.label_x + _LABEL_WIDTH_PX,
            e.label_y + _LABEL_HEIGHT_PX,
        )
        for e in graph.edges
        if e.label and e.label_x is not None and e.label_y is not None
    ]

    for edge in graph.edges:
        source = graph.node_by_id(edge.source_id)
        target = graph.node_by_id(edge.target_id)
        if source.shape_type == ShapeType.EXIT:
            logger.warning("Edge leaves an open arrow end %s; skipped", source.id)
            continue
        source_id = ids_by_node[edge.source_id]
        to_exit = target.shape_type == ShapeType.EXIT
        if to_exit and not (edge.edge_path and len(edge.edge_path) >= 2):
            logger.warning("Edge into open end %s has no route; skipped", target.id)
            continue
        target_id = 0 if to_exit else ids_by_node[edge.target_id]

        if (
            (edge.exact_route or to_exit)
            and edge.edge_path
            and len(edge.edge_path) >= 2
        ):
            path = edge.edge_path
            glued = False
            plain = not to_exit and ShapeType.INPUT_OUTPUT not in (
                source.shape_type,
                target.shape_type,
            )
            if len(path) == 2 and plain:
                s_idx = _nearest_site_docx(source, path[0])
                e_idx = _nearest_site_docx(target, path[1])
                sx, sy = _cxn_site_pos_docx(source, s_idx)
                ex, ey = _cxn_site_pos_docx(target, e_idx)
                glued = (
                    abs(sx - path[0][0]) < 0.5
                    and abs(sy - path[0][1]) < 0.5
                    and abs(ex - path[1][0]) < 0.5
                    and abs(ey - path[1][1]) < 0.5
                )
            name = f"edge-{edge.source_id}-{edge.target_id}"
            if glued:
                x, y, w, h, flip = _segment(sx, sy, ex, ey)
                children.append(
                    _connector_wsp(
                        shape_id=next_id,
                        name=name,
                        prst="straightConnector1",
                        x=emu(x),
                        y=emu(y),
                        cx=emu(w),
                        cy=emu(h),
                        flip=flip,
                        start=(source_id, s_idx),
                        end=(target_id, e_idx),
                    )
                )
            else:
                children.append(
                    _freeform_wsp(
                        next_id, name, [(emu(px), emu(py)) for px, py in path]
                    )
                )
            next_id += 1
            if edge.label and edge.label_x is not None and edge.label_y is not None:
                children.append(
                    _label_wsp(
                        edge.label,
                        next_id,
                        emu(edge.label_x),
                        emu(edge.label_y),
                        emu(_LABEL_WIDTH_PX),
                        emu(_LABEL_HEIGHT_PX),
                        max(int(12 * scale) - 1, 8),
                        align_right=edge.label_align_right,
                    )
                )
                next_id += 1
            continue

        # Back-edges and forward edges both route through Graphviz's own
        # collision-free ortho path (edge.edge_path) via the same
        # choose_connector_from_path() pipeline — see the matching
        # comment in pptx_shapes.add_diagram_to_slide() for why the old
        # hand-rolled "rail" special-case for back-edges was removed.
        avoid_boxes = [
            box
            for nid, box in node_boxes.items()
            if nid not in (edge.source_id, edge.target_id)
        ] + label_boxes
        preset, start_idx, end_idx, adjs, bend_count = choose_connector_from_path(
            edge.edge_path, source, target, avoid_boxes
        )

        x0, y0 = _cxn_site_pos_docx(source, start_idx)
        x1, y1 = _cxn_site_pos_docx(target, end_idx)
        x, y, w, h, flip = _segment(x0, y0, x1, y1)

        preset = assert_no_diagonal_connector(
            preset,
            emu(w),
            emu(h),
            f"{edge.source_id}->{edge.target_id}",
        )

        name_prefix = "backedge" if edge.is_back_edge else "edge"
        children.append(
            _connector_wsp(
                shape_id=next_id,
                name=f"{name_prefix}-{edge.source_id}-{edge.target_id}",
                prst=preset,
                x=emu(x),
                y=emu(y),
                cx=emu(w),
                cy=emu(h),
                flip=flip,
                start=(source_id, start_idx),
                end=(target_id, end_idx),
                adj=adjs if adjs else None,
            )
        )
        next_id += 1
        if edge.label and edge.label_x is not None and edge.label_y is not None:
            children.append(
                _label_wsp(
                    edge.label,
                    next_id,
                    emu(edge.label_x),
                    emu(edge.label_y),
                    emu(_LABEL_WIDTH_PX),
                    emu(_LABEL_HEIGHT_PX),
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
    sect_pr = body.find(f"{{{_NS_W}}}sectPr")
    if sect_pr is not None:
        sect_pr.addprevious(paragraph)
    else:
        body.append(paragraph)
    logger.info(
        "Injected 1 canvas with %d shapes/connectors into DOCX (%d nodes, %d edges)",
        next_id - 2,
        len(graph.nodes),
        len(graph.edges),
    )
