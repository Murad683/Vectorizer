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
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.shapes.autoshape import Shape
from pptx.shapes.connector import Connector
from pptx.slide import Slide
from pptx.util import Emu, Pt

from app.core.shape_geometry import DOCUMENT_TEXT_BOTTOM
from app.models.diagram import DiagramEdge, LayoutedGraph, LayoutedNode, ShapeType

logger = logging.getLogger(__name__)

SHAPE_TYPE_TO_MSO: dict[ShapeType, MSO_SHAPE] = {
    ShapeType.TERMINATOR: MSO_SHAPE.ROUNDED_RECTANGLE,
    ShapeType.PROCESS: MSO_SHAPE.RECTANGLE,
    ShapeType.DECISION: MSO_SHAPE.DIAMOND,
    ShapeType.INPUT_OUTPUT: MSO_SHAPE.PARALLELOGRAM,
    ShapeType.DOCUMENT: MSO_SHAPE.FLOWCHART_DOCUMENT,
}

# Layout px are treated as CSS px: 1 px = 9525 EMU (96 dpi).
_EMU_PER_PX = 9525

_FONT_SIZE = Pt(12)
_MIN_FONT_SIZE = Pt(8)
# Connection point indices on pptx autoshapes: 0=top, 1=left, 2=bottom, 3=right.
_CXN_TOP, _CXN_LEFT, _CXN_BOTTOM, _CXN_RIGHT = 0, 1, 2, 3

_MAX_BENT_BENDS = 4

_PARALLELOGRAM_SHEAR_RATIO = 0.3


def parallelogram_geometry_xml(width_emu: int, height_emu: int) -> str:
    """Custom geometry XML for an INPUT_OUTPUT (parallelogram) shape.

    Pins connection points 0 (top) and 2 (bottom) to the shape's true
    bounding-box horizontal center so connectors don't kink.
    """
    w, h = int(width_emu), int(height_emu)
    s = int(h * _PARALLELOGRAM_SHEAR_RATIO)
    return (
        f"<a:custGeom {nsdecls('a')}>"
        "<a:avLst/><a:gdLst/><a:ahLst/>"
        "<a:cxnLst>"
        f'<a:cxn ang="0"><a:pos x="{w // 2}" y="0"/></a:cxn>'
        f'<a:cxn ang="0"><a:pos x="{s // 2}" y="{h // 2}"/></a:cxn>'
        f'<a:cxn ang="0"><a:pos x="{w // 2}" y="{h}"/></a:cxn>'
        f'<a:cxn ang="0"><a:pos x="{w - s // 2}" y="{h // 2}"/></a:cxn>'
        "</a:cxnLst>"
        '<a:rect l="l" t="t" r="r" b="b"/>'
        "<a:pathLst>"
        f'<a:path w="{w}" h="{h}">'
        f'<a:moveTo><a:pt x="{s}" y="0"/></a:moveTo>'
        f'<a:lnTo><a:pt x="{w}" y="0"/></a:lnTo>'
        f'<a:lnTo><a:pt x="{w - s}" y="{h}"/></a:lnTo>'
        f'<a:lnTo><a:pt x="0" y="{h}"/></a:lnTo>'
        "<a:close/>"
        "</a:path>"
        "</a:pathLst>"
        "</a:custGeom>"
    )


def _apply_parallelogram_geometry(
    shape: Shape, width_emu: int, height_emu: int
) -> None:
    """Swap ``shape``'s preset parallelogram geometry for the custom one."""
    sp_pr = shape._element.spPr
    old_geom = sp_pr.find(qn("a:prstGeom"))
    new_geom = parse_xml(parallelogram_geometry_xml(width_emu, height_emu))
    if old_geom is not None:
        old_geom.addprevious(new_geom)
        sp_pr.remove(old_geom)
    else:
        sp_pr.insert(0, new_geom)


def _px_to_emu(px: float, scale: float) -> Emu:
    """Convert layout px to EMU, applying the fit-to-slide scale."""
    return Emu(int(px * scale * _EMU_PER_PX))


def _font_size_for(node: LayoutedNode, scale: float) -> Pt:
    """Pick a font size that fits the node's text into its (scaled) box."""
    size = int(_FONT_SIZE.pt * scale)
    approx_char_px = 0.66
    lines = node.text.split("\n")
    longest = max(len(line) for line in lines)
    # Text height in px: lines x line pitch (1.2 em; 1 pt = 4/3 px).
    usable_h = node.height * scale * 0.85
    if node.shape_type == ShapeType.DOCUMENT:
        usable_h *= DOCUMENT_TEXT_BOTTOM  # the wave is not part of the text area
    while size > int(_MIN_FONT_SIZE.pt):
        fits_w = longest * size * approx_char_px <= node.width * scale
        fits_h = len(lines) * size * 1.2 * 4 / 3 <= usable_h
        if fits_w and fits_h:
            break
        size -= 1
    return Pt(max(size, int(_MIN_FONT_SIZE.pt)))


_LABEL_HEIGHT_PX = 22.0
_LABEL_WIDTH_PX = 60.0


def _set_connector_adj(connector: Connector, name: str, val: int) -> None:
    """Set an explicit adjustment value on a connector's preset geometry."""
    prst_geom = connector._element.spPr.find(qn("a:prstGeom"))
    assert prst_geom is not None
    av_lst = prst_geom.find(qn("a:avLst"))
    if av_lst is None:
        av_lst = prst_geom.makeelement(qn("a:avLst"), {})
        prst_geom.append(av_lst)
    gd = av_lst.makeelement(qn("a:gd"), {"name": name, "fmla": f"val {val}"})
    av_lst.append(gd)


def _count_bends(path: list[list[float]]) -> int:
    """Count direction changes in an ortho path."""
    if len(path) < 3:
        return 0
    bends = 0
    for i in range(1, len(path) - 1):
        prev_horiz = abs(path[i][1] - path[i - 1][1]) < 0.5
        next_horiz = abs(path[i + 1][1] - path[i][1]) < 0.5
        if prev_horiz != next_horiz:
            bends += 1
    return bends


def choose_connector_from_path(
    path: list[list[float]] | None,
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    avoid_boxes: list[tuple[float, float, float, float]] | None = None,
) -> tuple[str, int, int, dict[str, int], int]:
    """Determine connector preset, connection sites, and adj values from
    the Graphviz-computed edge path.

    ``avoid_boxes`` (layout-px ``(x0, y0, x1, y1)`` rectangles — other
    nodes and edge labels) is only consulted when a bend has to be
    *forced* without real Graphviz path data to place it from (see
    ``_enforce_min_bends_for_axis``); it picks a default bend position
    that steers clear of them instead of blindly bisecting.

    Returns ``(preset, start_idx, end_idx, adjs, bend_count)`` where
    ``preset`` is the OOXML connector preset name, ``start_idx``/
    ``end_idx`` are connection-site indices, ``adjs`` maps adjustment
    names to values, and ``bend_count`` is how many bends were detected.
    """
    if path is None or len(path) < 2:
        return _choose_fallback(source_node, target_node)

    bends = _count_bends(path)

    start_pt = path[0]
    end_pt = path[-1]

    start_idx = _nearest_cxn_site(source_node, start_pt[0], start_pt[1])
    end_idx = _nearest_cxn_site(target_node, end_pt[0], end_pt[1])

    bends = _enforce_no_diagonal(bends, source_node, target_node, start_idx, end_idx)
    bends = _enforce_min_bends_for_axis(
        bends, source_node, target_node, start_idx, end_idx
    )

    if bends == 0:
        return "straightConnector1", start_idx, end_idx, {}, 0

    if bends > _MAX_BENT_BENDS:
        logger.warning(
            "Edge path has %d bends (max %d for OOXML presets); "
            "falling back to bentConnector5 with %d bends",
            bends,
            _MAX_BENT_BENDS,
            _MAX_BENT_BENDS,
        )
        bends = _MAX_BENT_BENDS

    preset_map = {
        1: "bentConnector2",
        2: "bentConnector3",
        3: "bentConnector4",
        4: "bentConnector5",
    }
    preset = preset_map[bends]

    adjs = _compute_adj_values(
        path, source_node, target_node, start_idx, end_idx, bends, avoid_boxes
    )

    return preset, start_idx, end_idx, adjs, bends


def _nearest_cxn_site(node: LayoutedNode, px: float, py: float) -> int:
    """Return the connection-site index (0=top, 1=left, 2=bottom, 3=right)
    on ``node`` closest to the point ``(px, py)``."""
    cx = node.x + node.width / 2
    cy = node.y + node.height / 2
    sites = {
        _CXN_TOP: (cx, node.y),
        _CXN_BOTTOM: (cx, node.y + node.height),
        _CXN_LEFT: (node.x, cy),
        _CXN_RIGHT: (node.x + node.width, cy),
    }
    best_idx = _CXN_TOP
    best_dist = float("inf")
    for idx, (sx, sy) in sites.items():
        d = (px - sx) ** 2 + (py - sy) ** 2
        if d < best_dist:
            best_dist = d
            best_idx = idx
    return best_idx


_FORCED_BEND_CANDIDATE_RATIOS = (
    50000,
    25000,
    75000,
    10000,
    90000,
    # Wider fallbacks: route the spine outside the direct start-end gap
    # entirely (a bigger dogleg) for cramped layouts — e.g. two nearby
    # decisions with several edge labels packed into a narrow corridor —
    # where every in-between position is occupied.
    150000,
    -50000,
    200000,
    -100000,
)


def _default_adj_for_forced_bend(
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    start_idx: int,
    end_idx: int,
    bend_count: int,
    avoid_boxes: list[tuple[float, float, float, float]] | None,
) -> dict[str, int]:
    """Pick adj1 for a 2-bend Z-shape forced without real path data.

    Tries the midpoint first, then increasingly off-center ratios,
    keeping the first one whose spine coordinate falls outside every
    box in ``avoid_boxes`` (typically this edge's own label, plus other
    nodes/labels the caller passed in).
    """
    if bend_count != 2:
        return {}
    sx, sy = _cxn_site_pos(source_node, start_idx)
    ex, ey = _cxn_site_pos(target_node, end_idx)
    horiz_spine = start_idx in (_CXN_LEFT, _CXN_RIGHT) and end_idx in (
        _CXN_LEFT,
        _CXN_RIGHT,
    )
    boxes = avoid_boxes or []
    # The middle segment only spans the source-to-target gap on its own
    # axis — a box is only actually in its way if the box also overlaps
    # that span on the OTHER axis (an unrelated box just happening to
    # share an x/y range far away on the other axis isn't a threat).
    x_lo, x_hi = min(sx, ex), max(sx, ex)
    y_lo, y_hi = min(sy, ey), max(sy, ey)
    for ratio in _FORCED_BEND_CANDIDATE_RATIOS:
        r = ratio / 100000
        if horiz_spine:
            spine = sx + r * (ex - sx)
            hit = any(
                b[0] < spine < b[2] and b[1] < y_hi and b[3] > y_lo for b in boxes
            )
        else:
            spine = sy + r * (ey - sy)
            hit = any(
                b[1] < spine < b[3] and b[0] < x_hi and b[2] > x_lo for b in boxes
            )
        if not hit:
            return {"adj1": ratio}
    return {"adj1": _FORCED_BEND_CANDIDATE_RATIOS[0]}


def _compute_adj_values(
    path: list[list[float]],
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    start_idx: int,
    end_idx: int,
    bend_count: int,
    avoid_boxes: list[tuple[float, float, float, float]] | None = None,
) -> dict[str, int]:
    """Compute OOXML adjustment values from the ortho waypoint path.

    Adj values position intermediate segments as ratios within the
    connector's bounding box. For bentConnector3 (2 bends), adj1
    controls the intermediate segment. For bentConnector4/5, adj1-adj3
    control successive intermediate segments.

    If there's no real path data to derive a bend from (``bend_count``
    was forced by ``_enforce_min_bends_for_axis`` onto what Graphviz
    computed as a plain straight line — see that function's docstring),
    fall back to a default that steers clear of ``avoid_boxes`` instead
    of blindly bisecting, since a naive midpoint can land the connector
    straight through its own edge label.
    """
    if bend_count < 2 or len(path) < 3:
        return _default_adj_for_forced_bend(
            source_node, target_node, start_idx, end_idx, bend_count, avoid_boxes
        )

    start_x, start_y = _cxn_site_pos(source_node, start_idx)
    end_x, end_y = _cxn_site_pos(target_node, end_idx)

    # Collect bend points (direction-change points) from the path.
    bend_pts: list[list[float]] = []
    for i in range(1, len(path) - 1):
        prev_horiz = abs(path[i][1] - path[i - 1][1]) < 0.5
        next_horiz = abs(path[i + 1][1] - path[i][1]) < 0.5
        if prev_horiz != next_horiz:
            bend_pts.append(path[i])

    adjs: dict[str, int] = {}
    first_seg_horiz = abs(path[1][1] - path[0][1]) < 0.5

    for k in range(min(bend_count - 1, 3)):
        if k >= len(bend_pts):
            break
        bp = bend_pts[k]
        adj_name = f"adj{k + 1}"

        if (k % 2 == 0) == first_seg_horiz:
            # This adj controls a vertical segment's x-position.
            span = end_x - start_x
            if abs(span) < 1.0:
                adjs[adj_name] = 50000
            else:
                adjs[adj_name] = round((bp[0] - start_x) / span * 100000)
        else:
            # This adj controls a horizontal segment's y-position.
            span = end_y - start_y
            if abs(span) < 1.0:
                adjs[adj_name] = 50000
            else:
                adjs[adj_name] = round((bp[1] - start_y) / span * 100000)

    return adjs


def _cxn_site_pos(node: LayoutedNode, idx: int) -> tuple[float, float]:
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


def _enforce_no_diagonal(
    bends: int,
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    start_idx: int,
    end_idx: int,
) -> int:
    """Bump bends to at least 1 if a straight connector would be diagonal.

    A straightConnector1 (0 bends) draws a literal straight line between
    the connector's bounding-box corners. If the connection-site positions
    differ in both x and y — even by sub-pixel amounts — Office renders
    an unavoidable diagonal. Promote to 1 bend (bentConnector2) so the
    connector gets an L-shaped route instead.

    Uses a generous EMU-level threshold: even a 0.5 px difference
    (4762 EMU) produces a visible diagonal in real Word/PowerPoint.
    """
    if bends > 0:
        return bends
    sx, sy = _cxn_site_pos(source_node, start_idx)
    ex, ey = _cxn_site_pos(target_node, end_idx)
    dx_emu = abs(sx - ex) * _EMU_PER_PX
    dy_emu = abs(sy - ey) * _EMU_PER_PX
    if dx_emu > 1 and dy_emu > 1:
        logger.info(
            "Promoting 0-bend to 1-bend: connection sites differ in both "
            "x (%.1f vs %.1f, %d EMU) and y (%.1f vs %.1f, %d EMU) "
            "— straight would be diagonal",
            sx,
            ex,
            int(dx_emu),
            sy,
            ey,
            int(dy_emu),
        )
        return 1
    return bends


def _enforce_min_bends_for_axis(
    bends: int,
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    start_idx: int,
    end_idx: int,
) -> int:
    """Bump bends to at least 2 if the chosen sites can't form a valid L.

    A single-bend elbow (bentConnector2) is a 2-segment path and can
    only connect sites on PERPENDICULAR axes (one top/bottom, the other
    left/right) — that pairing is what makes a single 90-degree turn
    reach from one to the other. If ``_nearest_cxn_site`` snapped BOTH
    ends onto the SAME axis (both left/right, or both top/bottom) —
    which happens easily for diamond/decision nodes, whose real
    Graphviz exit point sits on a slanted edge between two rectangle
    connection sites — a 1-bend route is geometrically impossible
    unless the two points already share the perpendicular coordinate
    (making it a straight line instead). Otherwise, only a 2-bend
    Z-shaped route can connect them without cutting diagonally through
    whatever happens to sit between the two connector sites.
    """
    same_axis_horiz = start_idx in (_CXN_LEFT, _CXN_RIGHT) and end_idx in (
        _CXN_LEFT,
        _CXN_RIGHT,
    )
    same_axis_vert = start_idx in (_CXN_TOP, _CXN_BOTTOM) and end_idx in (
        _CXN_TOP,
        _CXN_BOTTOM,
    )
    if not (same_axis_horiz or same_axis_vert):
        return bends
    sx, sy = _cxn_site_pos(source_node, start_idx)
    ex, ey = _cxn_site_pos(target_node, end_idx)
    # A generous tolerance, unlike _enforce_no_diagonal's sub-pixel one:
    # a diamond's real Graphviz exit point sits on its slanted edge, not
    # its bbox-side midpoint, so two nodes Graphviz connected with a
    # perfectly clean straight/near-straight line routinely "disagree"
    # by some tens of px once snapped to our 4 canonical rectangle
    # sites. Forcing a full Z-detour for every such disagreement does
    # more harm than good in tightly packed layouts (the detour can
    # itself cut through a third shape) for a mismatch that a plain
    # 1-bend L renders perfectly reasonably despite being a few px off
    # "true" perpendicularity. Reserve the 2-bend promotion for
    # genuinely large mismatches — the kind that would otherwise draw a
    # visibly wrong connector, not a rounding-level approximation.
    tolerance_emu = 20 * _EMU_PER_PX
    if same_axis_horiz:
        aligned = abs(sy - ey) * _EMU_PER_PX <= tolerance_emu
    else:
        aligned = abs(sx - ex) * _EMU_PER_PX <= tolerance_emu
    if aligned or bends >= 2:
        return bends
    logger.info(
        "Promoting %d-bend to 2-bend: connection sites (%d, %d) sit "
        "on the same axis but aren't aligned — a single bend can't "
        "connect them without an invalid route",
        bends,
        start_idx,
        end_idx,
    )
    return 2


def _choose_fallback(
    source_node: LayoutedNode, target_node: LayoutedNode
) -> tuple[str, int, int, dict[str, int], int]:
    """Fallback connector choice when no edge path is available."""
    source_cx = source_node.x + source_node.width / 2
    target_cx = target_node.x + target_node.width / 2
    if abs(source_cx - target_cx) < 1.0:
        return "straightConnector1", _CXN_BOTTOM, _CXN_TOP, {}, 0
    side = _CXN_LEFT if target_cx < source_cx else _CXN_RIGHT
    return "bentConnector2", side, _CXN_TOP, {}, 1


def _add_edge_label(
    slide: Slide,
    edge: DiagramEdge,
    scale: float,
    offset_x_emu: int,
    offset_y_emu: int,
) -> None:
    """Place a small borderless text box at the Graphviz-computed label position."""
    if edge.label_x is None or edge.label_y is None:
        return
    box = slide.shapes.add_textbox(
        Emu(offset_x_emu + int(edge.label_x * scale * _EMU_PER_PX)),
        Emu(offset_y_emu + int(edge.label_y * scale * _EMU_PER_PX)),
        _px_to_emu(_LABEL_WIDTH_PX, scale),
        _px_to_emu(_LABEL_HEIGHT_PX, scale),
    )
    frame = box.text_frame
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    frame.word_wrap = True
    if edge.label_align_right:
        frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
    run = frame.paragraphs[0].add_run()
    run.text = edge.label
    run.font.size = Pt(max(int(_FONT_SIZE.pt * scale) - 1, int(_MIN_FONT_SIZE.pt)))
    run.font.italic = True


class DiagonalConnectorError(RuntimeError):
    """A straightConnector1 would render as a diagonal line."""


def assert_no_diagonal_connector(
    preset: str,
    cx_emu: int,
    cy_emu: int,
    edge_label: str,
) -> str:
    """Hard invariant: a 0-bend connector must be axis-aligned.

    If ``preset`` is ``straightConnector1`` but both ``cx`` and ``cy``
    are non-zero (which would render as a diagonal line in Office),
    auto-corrects to ``bentConnector2`` (1 bend, L-shaped) and logs
    a warning. Returns the (possibly corrected) preset name.
    """
    if preset == "straightConnector1" and abs(cx_emu) > 1 and abs(cy_emu) > 1:
        logger.warning(
            "Diagonal invariant triggered for edge '%s': "
            "straightConnector1 with cx=%d, cy=%d — auto-correcting "
            "to bentConnector2",
            edge_label,
            cx_emu,
            cy_emu,
        )
        return "bentConnector2"
    return preset


_MSO_CONNECTOR_MAP: dict[str, MSO_CONNECTOR] = {
    "straightConnector1": MSO_CONNECTOR.STRAIGHT,
    "bentConnector2": MSO_CONNECTOR.ELBOW,
    "bentConnector3": MSO_CONNECTOR.ELBOW,
    "bentConnector4": MSO_CONNECTOR.ELBOW,
    "bentConnector5": MSO_CONNECTOR.ELBOW,
}


def _add_arrowhead(line_format: object) -> None:
    """Add a triangle arrowhead at the end of a line (idempotent)."""
    ln = line_format._get_or_add_ln()  # type: ignore[attr-defined]
    if ln.find(qn("a:tailEnd")) is None:
        ln.append(parse_xml(f'<a:tailEnd {nsdecls("a")} type="triangle"/>'))


def _add_freeform_edge(
    slide: Slide,
    edge: DiagramEdge,
    scale: float,
    offset_x_emu: int,
    offset_y_emu: int,
) -> None:
    """Draw ``edge.edge_path`` as an editable polyline with an arrowhead."""
    pts = [
        (
            offset_x_emu + int(x * scale * _EMU_PER_PX),
            offset_y_emu + int(y * scale * _EMU_PER_PX),
        )
        for x, y in edge.edge_path or []
    ]
    builder = slide.shapes.build_freeform(pts[0][0], pts[0][1], scale=1.0)
    builder.add_line_segments(pts[1:], close=False)
    shape = builder.convert_to_shape()
    shape.fill.background()
    shape.line.width = Pt(1.5)
    shape.line.color.rgb = RGBColor(0, 0, 0)
    shape.shadow.inherit = False
    _add_arrowhead(shape.line)
    shape.name = f"edge-{edge.source_id}-{edge.target_id}"


def _add_exact_edge(
    slide: Slide,
    edge: DiagramEdge,
    source: Shape,
    target: Shape,
    source_node: LayoutedNode,
    target_node: LayoutedNode,
    scale: float,
    offset_x_emu: int,
    offset_y_emu: int,
) -> None:
    """Draw an edge whose final orthogonal route was computed by the layout.

    A single straight segment ending exactly on two connection sites
    becomes a real glued connector (follows the shapes when dragged);
    anything with bends becomes an editable freeform polyline.
    """
    path = edge.edge_path or []
    plain = ShapeType.INPUT_OUTPUT not in (
        source_node.shape_type,
        target_node.shape_type,
    )
    # Parallelograms use custom connection sites that Office and other
    # viewers resolve differently, so edges touching them are drawn as
    # plain (unglued) lines that look identical everywhere.
    if len(path) == 2 and plain:
        s_idx = _nearest_cxn_site(source_node, path[0][0], path[0][1])
        e_idx = _nearest_cxn_site(target_node, path[1][0], path[1][1])
        sx, sy = _cxn_site_pos(source_node, s_idx)
        ex, ey = _cxn_site_pos(target_node, e_idx)
        if (
            abs(sx - path[0][0]) < 0.5
            and abs(sy - path[0][1]) < 0.5
            and abs(ex - path[1][0]) < 0.5
            and abs(ey - path[1][1]) < 0.5
        ):
            connector = slide.shapes.add_connector(
                MSO_CONNECTOR.STRAIGHT, Emu(0), Emu(0), Emu(0), Emu(0)
            )
            connector.begin_connect(source, s_idx)
            connector.end_connect(target, e_idx)
            connector.line.width = Pt(1.5)
            connector.line.color.rgb = RGBColor(0, 0, 0)
            _add_arrowhead(connector.line)
            connector.name = f"edge-{edge.source_id}-{edge.target_id}"
            return
    _add_freeform_edge(slide, edge, scale, offset_x_emu, offset_y_emu)


def add_diagram_to_slide(
    graph: LayoutedGraph,
    slide: Slide,
    *,
    offset_x_emu: int = 0,
    offset_y_emu: int = 0,
    max_width_emu: int | None = None,
    max_height_emu: int | None = None,
) -> int:
    """Draw the layouted graph onto ``slide`` as native editable shapes.

    Returns the drawn height in EMU (0 for an empty graph), so callers can
    place content (e.g. a caption) directly below the diagram.
    """
    if not graph.nodes:
        logger.warning("Empty graph; nothing added to slide")
        return 0

    scale = 1.0
    if max_width_emu is not None:
        scale = min(scale, max_width_emu / (graph.canvas_width * _EMU_PER_PX))
    if max_height_emu is not None:
        scale = min(scale, max_height_emu / (graph.canvas_height * _EMU_PER_PX))

    shapes_by_id = {}
    for node in graph.nodes:
        if node.shape_type == ShapeType.EXIT:
            continue  # an open arrow end is not drawn
        width_emu = _px_to_emu(node.width, scale)
        height_emu = _px_to_emu(node.height, scale)
        shape = slide.shapes.add_shape(
            SHAPE_TYPE_TO_MSO[node.shape_type] if node.framed else MSO_SHAPE.RECTANGLE,
            Emu(offset_x_emu + int(node.x * scale * _EMU_PER_PX)),
            Emu(offset_y_emu + int(node.y * scale * _EMU_PER_PX)),
            width_emu,
            height_emu,
        )
        if node.shape_type == ShapeType.INPUT_OUTPUT and node.framed:
            _apply_parallelogram_geometry(shape, int(width_emu), int(height_emu))
        if node.framed:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            shape.line.color.rgb = RGBColor(0x00, 0x00, 0x00)
            shape.line.width = Pt(1.5)
        else:  # drawn without an outline in the source: text only
            shape.fill.background()
            shape.line.fill.background()
        shape.shadow.inherit = False
        text_frame = shape.text_frame
        text_frame.word_wrap = True
        text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        font_size = _font_size_for(node, scale)
        for i, line in enumerate(node.text.split("\n")):
            paragraph = (
                text_frame.paragraphs[0] if i == 0 else text_frame.add_paragraph()
            )
            paragraph.alignment = PP_ALIGN.CENTER
            run = paragraph.add_run()
            run.text = line
            run.font.size = font_size
            run.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
        shapes_by_id[node.id] = shape

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

    exit_ids = {n.id for n in graph.nodes if n.shape_type == ShapeType.EXIT}
    for edge in graph.edges:
        source = shapes_by_id.get(edge.source_id)
        target = shapes_by_id.get(edge.target_id)
        if (
            source is not None
            and edge.target_id in exit_ids
            and edge.edge_path
            and len(edge.edge_path) >= 2
        ):
            _add_freeform_edge(slide, edge, scale, offset_x_emu, offset_y_emu)
            if edge.label:
                _add_edge_label(slide, edge, scale, offset_x_emu, offset_y_emu)
            continue
        if source is None or target is None:
            logger.warning(
                "Edge %s->%s references unknown node; skipped",
                edge.source_id,
                edge.target_id,
            )
            continue
        source_node = graph.node_by_id(edge.source_id)
        target_node = graph.node_by_id(edge.target_id)

        if edge.exact_route and edge.edge_path and len(edge.edge_path) >= 2:
            _add_exact_edge(
                slide,
                edge,
                source,
                target,
                source_node,
                target_node,
                scale,
                offset_x_emu,
                offset_y_emu,
            )
            if edge.label:
                _add_edge_label(slide, edge, scale, offset_x_emu, offset_y_emu)
            continue

        # Back-edges and forward edges both route through Graphviz's own
        # collision-free ortho path (edge.edge_path) via the same
        # choose_connector_from_path() pipeline. Back-edges used to be
        # special-cased onto a hand-rolled "virtual rail beyond the
        # rightmost shape" (pre-Graphviz, from before Phase 21) — but a
        # glued connector's bounding box can never actually extend past
        # its own two connection points, so that rail position was
        # unreachable in practice and the adj1 ratio meant to reach it
        # would blow up or clamp inside the connector's own tiny local
        # box, landing the line on top of whatever shape happened to sit
        # there. Graphviz's dot already routes back-edges cleanly around
        # unrelated nodes, so there is no need for a separate mechanism.
        avoid_boxes = [
            box
            for nid, box in node_boxes.items()
            if nid not in (edge.source_id, edge.target_id)
        ] + label_boxes
        preset, start_idx, end_idx, adjs, bend_count = choose_connector_from_path(
            edge.edge_path, source_node, target_node, avoid_boxes
        )
        mso_type = _MSO_CONNECTOR_MAP.get(preset, MSO_CONNECTOR.ELBOW)
        connector = slide.shapes.add_connector(mso_type, Emu(0), Emu(0), Emu(0), Emu(0))
        connector.begin_connect(source, start_idx)
        connector.end_connect(target, end_idx)

        # Set the OOXML preset name explicitly.
        prst_geom = connector._element.spPr.find(qn("a:prstGeom"))
        if prst_geom is not None:
            prst_geom.set("prst", preset)

        for adj_name, adj_val in adjs.items():
            _set_connector_adj(connector, adj_name, adj_val)

        xfrm = connector._element.spPr.find(qn("a:xfrm"))
        if xfrm is not None:
            ext = xfrm.find(qn("a:ext"))
            if ext is not None:
                corrected = assert_no_diagonal_connector(
                    preset,
                    int(ext.get("cx", "0")),
                    int(ext.get("cy", "0")),
                    f"{edge.source_id}->{edge.target_id}",
                )
                if corrected != preset:
                    preset = corrected
                    prst_geom = connector._element.spPr.find(qn("a:prstGeom"))
                    if prst_geom is not None:
                        prst_geom.set("prst", preset)

        connector.line.width = Pt(1.5)

        if edge.label:
            _add_edge_label(slide, edge, scale, offset_x_emu, offset_y_emu)

    logger.info(
        "Added %d shapes and %d connectors to slide",
        len(shapes_by_id),
        len(graph.edges),
    )
    return int(graph.canvas_height * scale * _EMU_PER_PX)
