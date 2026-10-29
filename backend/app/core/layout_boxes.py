"""Layout that preserves the source image's own arrangement.

Each node carries a bounding box (``box_2d``) from the source image —
either measured by ``diagram_detect`` (``graph.boxes_detected``: exact) or
guessed by the vision model (approximate). Instead of re-flowing the
diagram with Graphviz (which ignores the original shape, e.g. loops and
side branches), this module keeps the nodes where they were in the image
and then routes every edge as an orthogonal polyline with a small A*
router over a Hanan-style grid.

- **Exact boxes**: one uniform scale for the whole drawing (the original
  proportions and spacing are kept), tiny alignment snapping, and the
  original connector lines (``graph.guide_segments``) made cheap for the
  router, so routes follow what the author drew.
- **Approximate boxes**: generous snapping into rows/columns and spreading
  until nothing touches, to absorb the model's coordinate noise.

Edge labels are placed last, at the free spot next to the edge's first
segment that collides with no node, line or other label.

No external binaries and no API calls are involved. ``layout_from_boxes``
returns ``None`` when the graph lacks usable boxes; the caller then falls
back to the Graphviz layout.
"""

import heapq
import logging
import statistics
from dataclasses import dataclass

from app.core.rich_text import display_lines
from app.core.shape_geometry import DOCUMENT_TEXT_BOTTOM, document_bottom
from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    LayoutedGraph,
    LayoutedNode,
    PortSide,
    ShapeType,
)

logger = logging.getLogger(__name__)

_TARGET_NODE_WIDTH = 150.0
_NODE_GAP_APPROX = 40.0
_NODE_GAP_EXACT = 26.0
_STUB = 12.0
_INFLATE = 4.0
_BEND_COST = 100.0
_OVERLAP_COST_PER_PX = 3.0
_CROSS_COST = 400.0
# Soft clearance: a line closer than this to an unrelated symbol (or running
# parallel this close to another edge's line) still routes, but pays per px,
# so a clear corridor wins whenever one exists.
_CLEARANCE = 12.0
_NEAR_NODE_COST_PER_PX = 4.0
_NEAR_LINE_COST_PER_PX = 2.0
_RIPUP_ROUNDS = 8
_HINT_MISMATCH_COST = 250.0  # leaving a decision on another side than seen
_GUIDE_FACTOR = 0.05
_GUIDE_BEND_COST = 10.0
_MARGIN = 20.0
_ATTACH_TOL = 25.0
_NO_EVIDENCE_COST = 60.0
_SHARED_EXIT_COST = 1500.0
_LABEL_CHAR_PX = 8.0
_LABEL_H = 18.0
_RENDER_LABEL_W = 60.0  # label box width used by the renderers
_SNAP_APPROX = 0.2
_SNAP_EXACT = 0.1
_MAX_TEXT_UPSCALE = 2.5
# Drawing height (layout px, ~96 dpi) that still fits a portrait page at
# full size; DOCX diagrams are capped at 7.5 in.
_PAGE_CANVAS_H = 640.0

# N, E, S, W as (dx, dy); layout px have a top-left origin.
_DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]
_SIDE_NORMAL = {
    PortSide.TOP: (0, -1),
    PortSide.RIGHT: (1, 0),
    PortSide.BOTTOM: (0, 1),
    PortSide.LEFT: (-1, 0),
}
Point = tuple[float, float]
_State = tuple[int, int, int]  # grid x index, grid y index, direction
Segment = tuple[float, float, float, float]


@dataclass
class _Box:
    """A node rectangle in layout px (top-left origin)."""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def port(self, side: PortSide) -> Point:
        """Connection-site position on the given side."""
        if side == PortSide.TOP:
            return (self.cx, self.y)
        if side == PortSide.BOTTOM:
            return (self.cx, self.y + self.h)
        if side == PortSide.LEFT:
            return (self.x, self.cy)
        return (self.x + self.w, self.cy)


_SHAPE_MIN = {
    "terminator": (120.0, 44.0),
    "process": (120.0, 44.0),
    "input_output": (130.0, 44.0),
    "document": (130.0, 44.0),
    "decision": (150.0, 80.0),
}


def _min_size(
    text: str, shape: str, latex: str | None = None, framed: bool = True
) -> tuple[float, float]:
    """Smallest comfortable size for the node's text at 12 pt."""
    base_w, base_h = _SHAPE_MIN[shape]
    longest, n_lines = display_lines(text, latex)
    if not framed:
        # Bare text: hug it, so arrows end at the text, not in empty space.
        # A stacked fraction (with a root sign above it) needs ~2.5 lines.
        return longest * 8.0 + 6.0, 26.0 + (n_lines - 1) * 30.0
    needed = longest * 8.0 + 30.0
    height = base_h + (n_lines - 1) * 16.0
    if shape == "decision":
        needed *= 1.6  # text must fit inside the diamond's inner area
        height += (n_lines - 1) * 16.0
    elif shape == "document":
        height /= DOCUMENT_TEXT_BOTTOM  # text sits above the wave
    return max(base_w, needed), height


def _cluster(values: list[float], tol: float) -> list[float]:
    """Snap values that chain within ``tol`` of each other to their mean."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = list(values)
    group: list[int] = []
    for idx in order:
        if group and values[idx] - values[group[-1]] > tol:
            mean = sum(values[i] for i in group) / len(group)
            for i in group:
                out[i] = mean
            group = []
        group.append(idx)
    if group:
        mean = sum(values[i] for i in group) / len(group)
        for i in group:
            out[i] = mean
    return out


def _too_close(a: _Box, b: _Box, gap: float) -> bool:
    gx = max(a.x - (b.x + b.w), b.x - (a.x + a.w))
    gy = max(a.y - (b.y + b.h), b.y - (a.y + a.h))
    return gx < gap and gy < gap


def _guess_exit_box(graph: DiagramGraph, exit_id: str) -> list[int] | None:
    """A model forgot the open arrow end's box: put it just below the
    symbols that point at it (normalised 0-1000 like ``box_2d``)."""
    sources = [
        n.box_2d
        for e in graph.edges
        if e.target_id == exit_id
        for n in graph.nodes
        if n.id == e.source_id and n.box_2d
    ]
    if not sources:
        return None
    x = sum((b[1] + b[3]) / 2 for b in sources) / len(sources)
    y = min(1000, max(b[2] for b in sources) + 60)
    return [round(y), round(x), round(y), round(x)]


def _snap_exits_to_free_line_ends(
    ids: list[str],
    points: set[str],
    cxs: list[float],
    cys: list[float],
    ws: list[float],
    hs: list[float],
    vert: list[tuple[float, float, float]],
    horiz: list[tuple[float, float, float]],
) -> None:
    """Move each open arrow end onto the nearest end of an original line
    that touches no symbol — that is where the author's arrowhead is."""
    if not points or not (vert or horiz):
        return
    rects = [
        (cxs[i] - ws[i] / 2, cys[i] - hs[i] / 2, cxs[i] + ws[i] / 2, cys[i] + hs[i] / 2)
        for i, nid in enumerate(ids)
        if nid not in points
    ]

    def free(p: Point) -> bool:
        pad = _ATTACH_TOL
        return not any(
            r[0] - pad <= p[0] <= r[2] + pad and r[1] - pad <= p[1] <= r[3] + pad
            for r in rects
        )

    ends: list[Point] = [(x, y) for x, a, b in vert for y in (a, b)]
    ends += [(x, y) for y, a, b in horiz for x in (a, b)]
    ends = [p for p in ends if free(p)]
    for i, nid in enumerate(ids):
        if nid not in points or not ends:
            continue
        best = min(ends, key=lambda p: abs(p[0] - cxs[i]) + abs(p[1] - cys[i]))
        if abs(best[0] - cxs[i]) + abs(best[1] - cys[i]) <= 1.5 * _TARGET_NODE_WIDTH:
            cxs[i], cys[i] = best


def _place_nodes(
    graph: DiagramGraph,
) -> tuple[dict[str, _Box], list[Segment]] | None:
    """Scale/snap/spread the source boxes and guide lines.

    Returns ``(boxes, guides)`` in layout px, or None if boxes are unusable.
    """
    width = float(graph.image_width or 1000)
    height = float(graph.image_height or 1000)
    exact = graph.boxes_detected
    # Open arrow ends: positioned like nodes but zero-sized, never snapped
    # to the symbol grid and never pushed apart from symbols.
    points = {n.id for n in graph.nodes if n.shape_type == ShapeType.EXIT}
    raw: dict[str, tuple[float, float, float, float]] = {}
    for node in graph.nodes:
        b = node.box_2d
        if node.id in points:
            if b is None or len(b) != 4:
                b = _guess_exit_box(graph, node.id)
            if b is None:
                return None
            ymin, xmin, ymax, xmax = (float(v) for v in b)
            raw[node.id] = (
                (xmin + xmax) / 2000 * width,
                (ymin + ymax) / 2000 * height,
                0.0,
                0.0,
            )
            continue
        if b is None or len(b) != 4:
            return None
        ymin, xmin, ymax, xmax = (float(v) for v in b)
        if not (ymax > ymin and xmax > xmin):
            return None
        raw[node.id] = (
            xmin / 1000 * width,
            ymin / 1000 * height,
            (xmax - xmin) / 1000 * width,
            (ymax - ymin) / 1000 * height,
        )
    if len(raw) != len(graph.nodes):
        return None

    symbols = [n for n in graph.nodes if n.id not in points]
    if not symbols:
        return None
    median_w = statistics.median(raw[n.id][2] for n in symbols)
    scale = _TARGET_NODE_WIDTH / median_w
    if exact:
        # One uniform scale large enough that every node's text fits in
        # its ORIGINAL proportions (no per-node growth → nothing collides),
        # but not so large that the drawing outgrows a page's height: the
        # page would shrink it again, below a readable font size. Text that
        # still needs room gets it as extra WIDTH per node (pages are wider
        # than flowcharts); the cuts below keep neighbours apart.
        need = max(
            max(
                _min_size(n.text, n.shape_type.value, n.latex, n.framed)[0]
                / raw[n.id][2],
                _min_size(n.text, n.shape_type.value, n.latex, n.framed)[1]
                / raw[n.id][3],
            )
            for n in symbols
        )
        top = min(raw[n.id][1] for n in symbols)
        bottom = max(raw[n.id][1] + raw[n.id][3] for n in symbols)
        fit_page = _PAGE_CANVAS_H / max(bottom - top, 1.0)
        scale = min(max(scale, need), scale * _MAX_TEXT_UPSCALE, max(scale, fit_page))

    ids = [n.id for n in graph.nodes]
    cxs, cys, ws, hs = [], [], [], []
    for node in graph.nodes:
        x, y, w, h = raw[node.id]
        if node.id in points:
            ws.append(0.0)
            hs.append(0.0)
            cxs.append(x * scale)
            cys.append(y * scale)
            continue
        mw, mh = _min_size(node.text, node.shape_type.value, node.latex, node.framed)
        ws.append(max(w * scale, mw))
        hs.append(max(h * scale, mh))
        cxs.append((x + w / 2) * scale)
        cys.append((y + h / 2) * scale)

    # Guides (exact mode only), scaled like the nodes.
    vert: list[tuple[float, float, float]] = []  # (x, y0, y1)
    horiz: list[tuple[float, float, float]] = []  # (y, x0, x1)
    if exact and graph.guide_segments:
        for seg in graph.guide_segments:
            if len(seg) != 4:
                continue
            x0, y0, x1, y1 = (
                seg[0] / 1000 * width * scale,
                seg[1] / 1000 * height * scale,
                seg[2] / 1000 * width * scale,
                seg[3] / 1000 * height * scale,
            )
            if abs(x1 - x0) < abs(y1 - y0):
                vert.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
            else:
                horiz.append(((y0 + y1) / 2, min(x0, x1), max(x0, x1)))

    # Snap node centres AND guide lines together so an author's straight
    # arrow stays straight between the (snapped) shapes it connects.
    ratio = _SNAP_EXACT if exact else _SNAP_APPROX
    tol = ratio * _TARGET_NODE_WIDTH
    sym = [i for i, nid in enumerate(ids) if nid not in points]
    new_cxs, new_cys = list(cxs), list(cys)
    for i, v in zip(sym, _cluster([cxs[i] for i in sym], tol), strict=True):
        new_cxs[i] = v
    for i, v in zip(
        sym, _cluster([cys[i] for i in sym], tol * (1.0 if exact else 0.6)), strict=True
    ):
        new_cys[i] = v

    def follow(v: float, old: list[float], new: list[float]) -> float:
        # A guide moves with the node centre it was aligned with; guides
        # are never merged with each other (distinct parallel lines stay
        # distinct).
        best = min(range(len(old)), key=lambda i: abs(old[i] - v))
        return v + (new[best] - old[best]) if abs(old[best] - v) <= tol else v

    old_cx = [cxs[i] for i in sym]
    old_cy = [cys[i] for i in sym]
    vert = [(follow(v[0], old_cx, [new_cxs[i] for i in sym]), v[1], v[2]) for v in vert]
    horiz = [
        (follow(h[0], old_cy, [new_cys[i] for i in sym]), h[1], h[2]) for h in horiz
    ]
    cxs, cys = new_cxs, new_cys
    _snap_exits_to_free_line_ends(ids, points, cxs, cys, ws, hs, vert, horiz)

    boxes: dict[str, _Box] = {}
    gap = _NODE_GAP_EXACT if exact else _NODE_GAP_APPROX
    # Make room only where it is missing: each too-tight gap between two
    # symbols gets exactly the space it lacks, inserted as a cut — every
    # coordinate beyond the cut (symbols AND original lines) moves by that
    # amount. Spacious parts keep the source proportions, so the drawing
    # does not grow (and shrink on the page) more than its text requires.
    cuts: list[tuple[int, float, float]] = []  # (axis 0=x / 1=y, at, extra)

    def shift(v: float, axis: int) -> float:
        for cut_axis, at, extra in cuts:
            if cut_axis == axis and v > at:
                v += extra
        return v

    def build() -> list[_Box]:
        for i, nid in enumerate(ids):
            boxes[nid] = _Box(cxs[i] - ws[i] / 2, cys[i] - hs[i] / 2, ws[i], hs[i])
        return [boxes[nid] for nid in ids if nid not in points]

    for _ in range(4 * len(ids)):
        items = build()
        worst: tuple[float, int, float] | None = None  # (deficit, axis, at)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, c = items[i], items[j]
                if not _too_close(a, c, gap):
                    continue
                h_overlap = a.x < c.x + c.w and c.x < a.x + a.w
                v_overlap = a.y < c.y + c.h and c.y < a.y + a.h
                if h_overlap != v_overlap:
                    vertical = h_overlap  # stacked -> open a vertical gap
                else:
                    vertical = abs(c.cy - a.cy) >= abs(c.cx - a.cx)
                if vertical:
                    lo, hi = (a, c) if a.cy <= c.cy else (c, a)
                    deficit = gap - (hi.y - (lo.y + lo.h))
                    mid, c0, c1, axis = (lo.y + lo.h + hi.y) / 2, lo.cy, hi.cy, 1
                else:
                    lo, hi = (a, c) if a.cx <= c.cx else (c, a)
                    deficit = gap - (hi.x - (lo.x + lo.w))
                    mid, c0, c1, axis = (lo.x + lo.w + hi.x) / 2, lo.cx, hi.cx, 0
                if c1 - c0 < 0.02:
                    continue  # concentric: nothing a cut can separate
                # The cut must separate the two centres so only one side moves.
                at = min(max(mid, c0 + 0.01), c1 - 0.01)
                if deficit > 0.5 and (worst is None or deficit > worst[0]):
                    worst = (deficit, axis, at)
        # A symbol that grew for its text must not swallow an original line
        # running past it (e.g. a branch line beside a widened formula):
        # push the line out with a cut between the symbol's centre and it.
        for nb in items:
            for axis, lines in ((0, vert), (1, horiz)):
                for pos, l0, l1 in lines:
                    p = shift(pos, axis)
                    a0, a1 = shift(l0, 1 - axis), shift(l1, 1 - axis)
                    span0, span1 = (
                        (nb.y, nb.y + nb.h) if axis == 0 else (nb.x, nb.x + nb.w)
                    )
                    if min(a1, span1) - max(a0, span0) < 0.3 * (span1 - span0):
                        continue  # the line does not run alongside this symbol
                    start, size, centre = (
                        (nb.x, nb.w, nb.cx) if axis == 0 else (nb.y, nb.h, nb.cy)
                    )
                    if p > centre:  # line right of / below the centre: move it out
                        deficit = start + size + gap - p
                        at = max(p - 0.01, centre + 0.01)
                    else:  # line left of / above: move the symbol away from it
                        deficit = p + gap - start
                        at = min(p + 0.01, centre - 0.01)
                    if deficit > 0.5 and (worst is None or deficit > worst[0]):
                        worst = (deficit, axis, at)
        if worst is None:
            break
        extra, axis, at = worst[0] + 0.5, worst[1], worst[2]
        coords = cxs if axis == 0 else cys
        for i in range(len(coords)):
            if coords[i] > at:
                coords[i] += extra
        cuts.append((axis, at, extra))
    build()

    def sx(v: float) -> float:
        return shift(v, 0)

    def sy(v: float) -> float:
        return shift(v, 1)

    guides: list[Segment] = [(sx(x), sy(a), sx(x), sy(b)) for x, a, b in vert]
    guides += [(sx(a), sy(y), sx(b), sy(y)) for y, a, b in horiz]
    return boxes, guides


@dataclass(frozen=True)
class _Port:
    """A connection point on a node's border."""

    node: str
    side: PortSide
    point: Point  # where the line touches the outline
    evidence: bool  # an original line ends here

    @property
    def key(self) -> tuple[str, float, float]:
        return (self.node, round(self.point[0], 1), round(self.point[1], 1))

    def stub(self, box: "_Box") -> Point:
        """First grid point just outside the node's bounding box."""
        nx, ny = _SIDE_NORMAL[self.side]
        if nx == 0:
            edge_y = box.y if ny < 0 else box.y + box.h
            return (round(self.point[0], 2), round(edge_y + ny * _STUB, 2))
        edge_x = box.x if nx < 0 else box.x + box.w
        return (round(edge_x + nx * _STUB, 2), round(self.point[1], 2))


class _Router:
    """A* orthogonal edge router over a grid built from the node boxes."""

    def __init__(
        self,
        boxes: dict[str, _Box],
        guides: list[Segment],
        diamonds: set[str] | None = None,
        documents: set[str] | None = None,
    ) -> None:
        self.boxes = boxes
        self.diamonds = diamonds or set()
        self.documents = documents or set()
        self.obstacles = {
            nid: (
                b.x - _INFLATE,
                b.y - _INFLATE,
                b.x + b.w + _INFLATE,
                b.y + b.h + _INFLATE,
            )
            for nid, b in boxes.items()
            if b.w > 0 or b.h > 0  # an open arrow end blocks nothing
        }
        self.vguides: list[tuple[float, float, float]] = []
        self.hguides: list[tuple[float, float, float]] = []
        for x0, y0, x1, y1 in guides:
            if abs(x0 - x1) < 0.01:
                self.vguides.append((round(x0, 2), min(y0, y1), max(y0, y1)))
            else:
                self.hguides.append((round(y0, 2), min(x0, x1), max(x0, x1)))
        self.ports = self._build_ports()

        xs: set[float] = set()
        ys: set[float] = set()
        for b in boxes.values():
            for dx in (-12.0, -_STUB, -28.0, 0.0, b.w + 12.0, b.w + _STUB, b.w + 28.0):
                xs.add(round(b.x + dx, 2))
            xs.add(round(b.cx, 2))
            for dy in (-12.0, -_STUB, -28.0, 0.0, b.h + 12.0, b.h + _STUB, b.h + 28.0):
                ys.add(round(b.y + dy, 2))
            ys.add(round(b.cy, 2))
        for gx, g0, g1 in self.vguides:
            xs.add(gx)
            ys.update((round(g0, 2), round(g1, 2)))
        for gy, g0, g1 in self.hguides:
            ys.add(gy)
            xs.update((round(g0, 2), round(g1, 2)))
        for plist in self.ports.values():
            for port in plist:
                sx, sy = port.stub(boxes[port.node])
                xs.add(sx)
                ys.add(sy)
        all_x0 = min(b.x for b in boxes.values())
        all_x1 = max(b.x + b.w for b in boxes.values())
        all_y0 = min(b.y for b in boxes.values())
        all_y1 = max(b.y + b.h for b in boxes.values())
        xs.update((round(all_x0 - 3 * _STUB, 2), round(all_x1 + 3 * _STUB, 2)))
        ys.update((round(all_y0 - 3 * _STUB, 2), round(all_y1 + 3 * _STUB, 2)))
        self.xs = sorted(xs)
        self.ys = sorted(ys)
        self.xi = {v: i for i, v in enumerate(self.xs)}
        self.yi = {v: i for i, v in enumerate(self.ys)}
        # Segments already used by routed edges:
        # (x0, y0, x1, y1, target_id, edge_key), x0<=x1, y0<=y1.
        self.used: list[tuple[float, float, float, float, str, int]] = []
        self.port_use: dict[tuple[str, float, float], dict[str, int]] = {}
        self.side_out: dict[tuple[str, PortSide], int] = {}
        # The edge being routed: its own end symbols are exempt from the
        # clearance penalty (a line must touch them).
        self._ends: tuple[str, str] = ("", "")
        self.last_cost = 0.0  # cost of the route() result just returned

    # -- ports --------------------------------------------------------------
    def _outline_point(self, nid: str, side: PortSide, along: float) -> Point:
        """Border point on ``side`` at coordinate ``along`` (x for top/bottom,
        y for left/right); follows the slanted outline of a diamond and the
        wavy bottom of a document symbol."""
        b = self.boxes[nid]
        if side in (PortSide.TOP, PortSide.BOTTOM):
            x = min(max(along, b.x), b.x + b.w)
            if nid in self.diamonds:
                dy = (b.h / 2) * (1 - min(abs(x - b.cx) / (b.w / 2), 1.0))
                y = b.cy - dy if side == PortSide.TOP else b.cy + dy
            elif nid in self.documents and side == PortSide.BOTTOM:
                y = b.y + b.h * document_bottom((x - b.x) / b.w)
            else:
                y = b.y if side == PortSide.TOP else b.y + b.h
            return (x, y)
        y = min(max(along, b.y), b.y + b.h)
        if nid in self.diamonds:
            dx = (b.w / 2) * (1 - min(abs(y - b.cy) / (b.h / 2), 1.0))
            x = b.cx - dx if side == PortSide.LEFT else b.cx + dx
        else:
            x = b.x if side == PortSide.LEFT else b.x + b.w
        return (x, y)

    def _build_ports(self) -> dict[str, list[_Port]]:
        """Side midpoints plus every point where an original line ends."""
        tol = _ATTACH_TOL
        ports: dict[str, list[_Port]] = {}
        for nid, b in self.boxes.items():
            if b.w == 0 and b.h == 0:
                # Open arrow end: arrive from any side, exactly at the point.
                ports[nid] = [_Port(nid, side, (b.x, b.y), False) for side in PortSide]
                continue
            hits: list[tuple[PortSide, float]] = []
            for gx, g0, g1 in self.vguides:
                if b.x - 2 <= gx <= b.x + b.w + 2:
                    if abs(g1 - b.y) < tol:
                        hits.append((PortSide.TOP, gx))
                    if abs(g0 - (b.y + b.h)) < tol:
                        hits.append((PortSide.BOTTOM, gx))
            for gy, g0, g1 in self.hguides:
                if b.y - 2 <= gy <= b.y + b.h + 2:
                    if abs(g1 - b.x) < tol:
                        hits.append((PortSide.LEFT, gy))
                    if abs(g0 - (b.x + b.w)) < tol:
                        hits.append((PortSide.RIGHT, gy))
            # An arrow drawn onto the connector just outside a side (e.g. a
            # loop landing on the line into a decision's top) is evidence for
            # that side's centre: lines ending there arrive through it.
            reach = 3 * tol
            for gy, g0, g1 in self.hguides:
                if any(abs(end - b.cx) < tol for end in (g0, g1)):
                    if b.y - reach < gy < b.y:
                        hits.append((PortSide.TOP, b.cx))
                    if b.y + b.h < gy < b.y + b.h + reach:
                        hits.append((PortSide.BOTTOM, b.cx))
            for gx, g0, g1 in self.vguides:
                if any(abs(end - b.cy) < tol for end in (g0, g1)):
                    if b.x - reach < gx < b.x:
                        hits.append((PortSide.LEFT, b.cy))
                    if b.x + b.w < gx < b.x + b.w + reach:
                        hits.append((PortSide.RIGHT, b.cy))
            plist: list[_Port] = []
            for side in PortSide:
                mid = b.cx if side in (PortSide.TOP, PortSide.BOTTOM) else b.cy
                side_hits = [a for s, a in hits if s == side]
                centre_hit = any(abs(a - mid) <= 6 for a in side_hits)
                plist.append(
                    _Port(nid, side, self._outline_point(nid, side, mid), centre_hit)
                )
                taken = [mid]
                for a in sorted(side_hits):
                    if all(abs(a - t) > 6 for t in taken):
                        taken.append(a)
                        plist.append(
                            _Port(nid, side, self._outline_point(nid, side, a), True)
                        )
            ports[nid] = plist
        return ports

    # -- geometry helpers -------------------------------------------------
    def _clear(self, p: Point, q: Point, skip: tuple[str, ...] = ()) -> bool:
        """True if the axis-aligned segment avoids every inflated node."""
        lo_x, hi_x = min(p[0], q[0]), max(p[0], q[0])
        lo_y, hi_y = min(p[1], q[1]), max(p[1], q[1])
        for nid, (x0, y0, x1, y1) in self.obstacles.items():
            if nid in skip:
                continue
            if lo_x < x1 and hi_x > x0 and lo_y < y1 and hi_y > y0:
                return False
        return True

    def _on_guide(self, p: Point, q: Point) -> bool:
        """True if the step runs along one of the author's original lines."""
        if abs(p[0] - q[0]) < 0.01:
            lo, hi = min(p[1], q[1]), max(p[1], q[1])
            return any(
                abs(gx - p[0]) < 1.5 and g0 - 4 <= lo and hi <= g1 + 4
                for gx, g0, g1 in self.vguides
            )
        lo, hi = min(p[0], q[0]), max(p[0], q[0])
        return any(
            abs(gy - p[1]) < 1.5 and g0 - 4 <= lo and hi <= g1 + 4
            for gy, g0, g1 in self.hguides
        )

    def _guide_through(self, p: Point, vertical: bool) -> bool:
        """True if an original line of the given orientation passes ``p``."""
        if vertical:
            return any(
                abs(gx - p[0]) < 1.5 and g0 - 4 <= p[1] <= g1 + 4
                for gx, g0, g1 in self.vguides
            )
        return any(
            abs(gy - p[1]) < 1.5 and g0 - 4 <= p[0] <= g1 + 4
            for gy, g0, g1 in self.hguides
        )

    def _penalty(self, p: Point, q: Point, target: str) -> float:
        """Extra cost for running along / crossing already routed edges."""
        lo_x, hi_x = min(p[0], q[0]), max(p[0], q[0])
        lo_y, hi_y = min(p[1], q[1]), max(p[1], q[1])
        vertical = abs(p[0] - q[0]) < 0.01
        cost = 0.0
        for ux0, uy0, ux1, uy1, u_target, _ in self.used:
            if u_target == target:
                continue  # edges into the same node may merge freely
            u_vertical = abs(ux0 - ux1) < 0.01
            if vertical and u_vertical:
                gap = abs(ux0 - p[0])
                run = max(0.0, min(hi_y, uy1) - max(lo_y, uy0))
                if gap < 1.0:
                    cost += _OVERLAP_COST_PER_PX * run
                elif gap < _CLEARANCE:
                    cost += _NEAR_LINE_COST_PER_PX * run
            elif not vertical and not u_vertical:
                gap = abs(uy0 - p[1])
                run = max(0.0, min(hi_x, ux1) - max(lo_x, ux0))
                if gap < 1.0:
                    cost += _OVERLAP_COST_PER_PX * run
                elif gap < _CLEARANCE:
                    cost += _NEAR_LINE_COST_PER_PX * run
            # Perpendicular: half-open along the step, so a crossing that
            # falls exactly on a grid vertex (where the path is split into
            # two steps) is charged exactly once instead of never.
            elif vertical:  # p-q vertical, u horizontal
                if lo_y <= uy0 < hi_y and ux0 < p[0] < ux1:
                    cost += _CROSS_COST
            else:
                if lo_x <= ux0 < hi_x and uy0 < p[1] < uy1:
                    cost += _CROSS_COST
        return cost

    def _near_nodes(self, p: Point, q: Point) -> float:
        """Length of the segment running inside other symbols' clearance."""
        lo_x, hi_x = min(p[0], q[0]), max(p[0], q[0])
        lo_y, hi_y = min(p[1], q[1]), max(p[1], q[1])
        total = 0.0
        for nid, b in self.boxes.items():
            if nid in self._ends or (b.w == 0 and b.h == 0):
                continue
            x0, y0 = b.x - _CLEARANCE, b.y - _CLEARANCE
            x1, y1 = b.x + b.w + _CLEARANCE, b.y + b.h + _CLEARANCE
            if hi_x - lo_x < 0.01:  # vertical
                if x0 < lo_x < x1:
                    total += max(0.0, min(hi_y, y1) - max(lo_y, y0))
            elif y0 < lo_y < y1:
                total += max(0.0, min(hi_x, x1) - max(lo_x, x0))
        return total

    def _step_cost(self, p: Point, q: Point, target: str) -> float:
        length = abs(q[0] - p[0]) + abs(q[1] - p[1])
        if self._on_guide(p, q):
            length *= _GUIDE_FACTOR
        near = _NEAR_NODE_COST_PER_PX * self._near_nodes(p, q)
        return length + near + self._penalty(p, q, target)

    def _port_penalty(
        self,
        port: _Port,
        role: str,
        hint: PortSide | None,
        toward: tuple[float, float] | None,
    ) -> float:
        pen = 0.0
        normal = _SIDE_NORMAL[port.side]
        if hint is not None and port.side != hint:
            pen += _HINT_MISMATCH_COST
        if (
            toward is not None
            and hint is None
            and normal[0] * toward[0] + normal[1] * toward[1] <= 0
        ):
            pen += 80.0
        has_evidence = any(p.evidence for p in self.ports[port.node])
        if has_evidence and not port.evidence:
            pen += _NO_EVIDENCE_COST
        use = self.port_use.get(port.key, {})
        other = "in" if role == "out" else "out"
        if use.get(other):
            pen += 300.0
        if role == "out":
            if use.get("out"):
                # Two exits from one point make branch labels ambiguous.
                pen += _SHARED_EXIT_COST
            elif self.side_out.get((port.node, port.side)):
                pen += 150.0
        elif use.get("in"):
            pen += 40.0
        return pen

    # -- routing ----------------------------------------------------------
    def _direct(
        self, edge: DiagramEdge, is_back: bool
    ) -> tuple[float, list[Point], _Port, _Port] | None:
        """Cheapest single straight segment between facing, aligned ports."""
        self._ends = (edge.source_id, edge.target_id)
        sb, tb = self.boxes[edge.source_id], self.boxes[edge.target_id]
        to_target = (tb.cx - sb.cx, tb.cy - sb.cy)
        best: tuple[float, list[Point], _Port, _Port] | None = None
        for sp in self.ports[edge.source_id]:
            n = _SIDE_NORMAL[sp.side]
            for tp in self.ports[edge.target_id]:
                if _SIDE_NORMAL[tp.side] != (-n[0], -n[1]):
                    continue
                p, q = sp.point, tp.point
                along = (q[0] - p[0]) * n[0] + (q[1] - p[1]) * n[1]
                across = abs((q[0] - p[0]) * n[1]) + abs((q[1] - p[1]) * n[0])
                if along <= 0 or across > 0.5:
                    continue
                q = (p[0] + n[0] * along, p[1] + n[1] * along)
                if not self._clear(p, q, skip=(edge.source_id, edge.target_id)):
                    continue
                cost = self._step_cost(p, q, edge.target_id)
                cost += self._port_penalty(
                    sp, "out", edge.source_port, None if is_back else to_target
                )
                cost += self._port_penalty(
                    tp,
                    "in",
                    None,
                    None if is_back else (-to_target[0], -to_target[1]),
                )
                if best is None or cost < best[0]:
                    best = (cost, [p, q], sp, tp)
        return best

    def route(
        self,
        edge: DiagramEdge,
        is_back: bool,
    ) -> tuple[list[Point], _Port, _Port] | None:
        """Route one edge; returns (polyline, source_port, target_port)."""
        self._ends = (edge.source_id, edge.target_id)
        sb, tb = self.boxes[edge.source_id], self.boxes[edge.target_id]
        to_target = (tb.cx - sb.cx, tb.cy - sb.cy)
        to_source = (-to_target[0], -to_target[1])

        starts: list[tuple[float, Point, int, _Port]] = []
        for port in self.ports[edge.source_id]:
            stub = port.stub(sb)
            if stub[0] not in self.xi or stub[1] not in self.yi:
                continue
            if not self._clear(stub, stub):
                continue
            pen = self._port_penalty(
                port, "out", edge.source_port, None if is_back else to_target
            )
            seg = abs(stub[0] - port.point[0]) + abs(stub[1] - port.point[1])
            starts.append((pen + seg, stub, _DIRS.index(_SIDE_NORMAL[port.side]), port))

        goals: dict[Point, list[tuple[float, _Port, int]]] = {}
        for port in self.ports[edge.target_id]:
            stub = port.stub(tb)
            n = _SIDE_NORMAL[port.side]
            pen = self._port_penalty(port, "in", None, None if is_back else to_source)
            seg = abs(stub[0] - port.point[0]) + abs(stub[1] - port.point[1])
            # Arriving moves INTO the port, i.e. against the outward normal.
            goals.setdefault(stub, []).append(
                (pen + seg, port, _DIRS.index((-n[0], -n[1])))
            )

        direct = self._direct(edge, is_back)
        goal_pts = list(goals)

        def h(p: Point) -> float:
            return (
                min(abs(p[0] - g[0]) + abs(p[1] - g[1]) for g in goal_pts)
                * _GUIDE_FACTOR
            )

        dist: dict[_State, float] = {}
        parent: dict[_State, _State | None] = {}
        start_port: dict[_State, _Port] = {}
        heap: list[tuple[float, float, _State]] = []
        for pen, stub, d, port in starts:
            st = (self.xi[stub[0]], self.yi[stub[1]], d)
            if pen < dist.get(st, float("inf")):
                dist[st] = pen
                parent[st] = None
                start_port[st] = port
                heapq.heappush(heap, (pen + h(stub), pen, st))

        best_cost = direct[0] if direct else float("inf")
        best: tuple[_State, _Port] | None = None
        while heap:
            f, g, st = heapq.heappop(heap)
            if f >= best_cost:
                break
            if g > dist.get(st, float("inf")):
                continue
            ix, iy, d = st
            here = (self.xs[ix], self.ys[iy])
            for gpen, gport, inward in goals.get(here, ()):
                total = g + gpen + (0.0 if d == inward else _BEND_COST)
                if total < best_cost:
                    best_cost = total
                    best = (st, gport)
            for nd in range(4):
                if (nd + 2) % 4 == d:
                    continue
                dx, dy = _DIRS[nd]
                jx, jy = ix + dx, iy + dy
                if not (0 <= jx < len(self.xs) and 0 <= jy < len(self.ys)):
                    continue
                there = (self.xs[jx], self.ys[jy])
                if not self._clear(here, there):
                    continue
                step = self._step_cost(here, there, edge.target_id)
                if nd != d:
                    # Turning at a corner of the author's own lines is cheap.
                    if self._on_guide(here, there) and self._guide_through(
                        here, vertical=_DIRS[d][0] == 0
                    ):
                        step += _GUIDE_BEND_COST
                    else:
                        step += _BEND_COST
                ng = g + step
                nst = (jx, jy, nd)
                if ng < dist.get(nst, float("inf")):
                    dist[nst] = ng
                    parent[nst] = st
                    start_port[nst] = start_port[st]
                    heapq.heappush(heap, (ng + h(there), ng, nst))

        self.last_cost = best_cost
        if best is None:
            if direct is not None:
                return direct[1], direct[2], direct[3]
            return None
        end_state, tport = best
        pts: list[Point] = []
        cur: _State | None = end_state
        while cur is not None:
            pts.append((self.xs[cur[0]], self.ys[cur[1]]))
            cur = parent[cur]
        pts.reverse()
        sport = start_port[end_state]
        path = [sport.point, *pts, tport.point]
        return _simplify(path), sport, tport

    def commit(
        self,
        edge: DiagramEdge,
        path: list[Point],
        s: _Port,
        t: _Port,
        key: int = -1,
    ) -> None:
        """Record a routed edge so later edges avoid/penalise it."""
        for p, q in zip(path, path[1:], strict=False):
            self.used.append(
                (
                    min(p[0], q[0]),
                    min(p[1], q[1]),
                    max(p[0], q[0]),
                    max(p[1], q[1]),
                    edge.target_id,
                    key,
                )
            )
        self._count(s, t, +1)

    def path_cost(
        self, edge: DiagramEdge, path: list[Point], s: _Port, t: _Port, is_back: bool
    ) -> float:
        """What ``route`` would charge for an existing ``path`` right now
        (same terms: ports, length, clearance, conflicts, bends)."""
        self._ends = (edge.source_id, edge.target_id)
        sb, tb = self.boxes[edge.source_id], self.boxes[edge.target_id]
        to_target = (tb.cx - sb.cx, tb.cy - sb.cy)
        cost = self._port_penalty(
            s, "out", edge.source_port, None if is_back else to_target
        )
        cost += self._port_penalty(
            t, "in", None, None if is_back else (-to_target[0], -to_target[1])
        )
        for p, q in zip(path, path[1:], strict=False):
            cost += self._step_cost(p, q, edge.target_id)
        return cost + _BEND_COST * max(0, len(path) - 2)

    def uncommit(self, key: int, s: _Port, t: _Port) -> None:
        """Forget a routed edge (for rip-up and re-route)."""
        self.used = [u for u in self.used if u[5] != key]
        self._count(s, t, -1)

    def _count(self, s: _Port, t: _Port, delta: int) -> None:
        self.port_use.setdefault(s.key, {}).setdefault("out", 0)
        self.port_use[s.key]["out"] += delta
        self.port_use.setdefault(t.key, {}).setdefault("in", 0)
        self.port_use[t.key]["in"] += delta
        side = (s.node, s.side)
        self.side_out[side] = self.side_out.get(side, 0) + delta


def _conflicts(path: list[Point], others: list[list[Point]]) -> int:
    """Crossings and collinear overlaps between ``path`` and ``others``."""
    count = 0
    for a, b in zip(path, path[1:], strict=False):
        va = abs(a[0] - b[0]) < 0.5
        for other in others:
            for c, d in zip(other, other[1:], strict=False):
                vc = abs(c[0] - d[0]) < 0.5
                if va == vc:
                    k = 0 if va else 1
                    if abs(a[k] - c[k]) < 1:
                        lo = max(min(a[1 - k], b[1 - k]), min(c[1 - k], d[1 - k]))
                        hi = min(max(a[1 - k], b[1 - k]), max(c[1 - k], d[1 - k]))
                        count += hi - lo > 1
                    continue
                v, h = ((a, b), (c, d)) if va else ((c, d), (a, b))
                x, y = v[0][0], h[0][1]
                count += (
                    min(h[0][0], h[1][0]) + 0.5 < x < max(h[0][0], h[1][0]) - 0.5
                    and min(v[0][1], v[1][1]) + 0.5 < y < max(v[0][1], v[1][1]) - 0.5
                )
    return count


def _on_segment(p: Point, a: Point, b: Point, interior: bool) -> bool:
    """True if ``p`` lies on the axis-aligned segment a-b (strictly between
    its ends when ``interior``)."""
    tol = 0.6
    if abs(a[0] - b[0]) < 0.01:  # vertical
        lo, hi = min(a[1], b[1]), max(a[1], b[1])
        inside = (
            lo + tol < p[1] < hi - tol if interior else lo - tol <= p[1] <= hi + tol
        )
        return abs(p[0] - a[0]) < tol and inside
    lo, hi = min(a[0], b[0]), max(a[0], b[0])
    inside = lo + tol < p[0] < hi - tol if interior else lo - tol <= p[0] <= hi + tol
    return abs(p[1] - a[1]) < tol and inside


def _covered(a: Point, b: Point, path: list[Point]) -> bool:
    """True if the whole segment a-b runs along ``path``."""
    n = max(2, int((abs(a[0] - b[0]) + abs(a[1] - b[1])) / 4))
    for k in range(n + 1):
        t = k / n
        p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        if not any(
            _on_segment(p, c, d, interior=False)
            for c, d in zip(path, path[1:], strict=False)
        ):
            return False
    return True


def _passes_straight(p: Point, path: list[Point]) -> tuple[float, float] | None:
    """Direction of ``path`` where it runs straight THROUGH ``p`` (``p`` is
    inside one of its segments, not at an end or a corner), else None."""
    for c, d in zip(path, path[1:], strict=False):
        if _on_segment(p, c, d, interior=True):
            return (d[0] - c[0], d[1] - c[1])
    return None


def _cut_at_merges(
    edges: list[DiagramEdge], routed: dict[int, tuple[list[Point], bool]]
) -> None:
    """End a merging arrow where it meets the line it merges into.

    Edges into the same symbol may share their last stretch (a loop joining
    the connector above a decision, two print blocks joining the line into
    "Son"). Drawn in full, the joining line hides under the other one and
    its arrowhead disappears into the symbol's arrowhead. Textbooks draw it
    the other way: the joining line stops at the junction with its OWN
    arrowhead pointing at the line it meets, and that line carries on to
    the symbol. An edge is cut at the first point where it arrives from the
    side onto a line of another edge into the same target that runs
    straight through that point, and from where on it only retraces that
    line. Lines that turn a corner together are not a junction.
    """
    groups: dict[str, list[int]] = {}
    for i in routed:
        if edges[i].source_id != edges[i].target_id:
            groups.setdefault(edges[i].target_id, []).append(i)
    for members in groups.values():
        if len(members) < 2:
            continue
        for i in sorted(members):
            path, back = routed[i]
            others = [routed[j][0] for j in members if j != i]
            # Corners of the other lines that lie on this one are candidate
            # junctions too (a line can meet ours in the middle of a run).
            refined: list[Point] = [path[0]]
            for a, b in zip(path, path[1:], strict=False):
                extra = [
                    q
                    for other in others
                    for q in other
                    if _on_segment(q, a, b, interior=True)
                ]
                extra.sort(key=lambda q: abs(q[0] - a[0]) + abs(q[1] - a[1]))
                refined += extra + [b]
            for k in range(1, len(refined) - 1):
                junction = refined[k]
                come = (
                    junction[0] - refined[k - 1][0],
                    junction[1] - refined[k - 1][1],
                )
                if abs(come[0]) + abs(come[1]) < 0.01:
                    continue
                for other in others:
                    through = _passes_straight(junction, other)
                    if through is None:
                        continue
                    if abs(come[0] * through[1] - come[1] * through[0]) < 0.01:
                        continue  # same direction: running along it, not joining
                    tail = list(zip(refined[k:], refined[k + 1 :], strict=False))
                    if all(_covered(a, b, other) for a, b in tail):
                        routed[i] = (_simplify(refined[: k + 1]), back)
                        break
                else:
                    continue
                break


def _simplify(path: list[Point]) -> list[Point]:
    """Drop duplicate and collinear interior points."""
    dedup = [path[0]]
    for p in path[1:]:
        if abs(p[0] - dedup[-1][0]) > 0.01 or abs(p[1] - dedup[-1][1]) > 0.01:
            dedup.append(p)
    if len(dedup) <= 2:
        return dedup
    out = [dedup[0]]
    for i in range(1, len(dedup) - 1):
        a, b, c = out[-1], dedup[i], dedup[i + 1]
        same_x = abs(a[0] - b[0]) < 0.01 and abs(b[0] - c[0]) < 0.01
        same_y = abs(a[1] - b[1]) < 0.01 and abs(b[1] - c[1]) < 0.01
        if not (same_x or same_y):
            out.append(b)
    out.append(dedup[-1])
    return out


def _self_loop(b: _Box) -> list[Point]:
    right = (b.x + b.w, b.cy)
    return [
        right,
        (right[0] + 30, right[1]),
        (right[0] + 30, b.y - 30),
        (b.cx, b.y - 30),
        (b.cx, b.y),
    ]


def _rect_hits_segment(
    r: tuple[float, float, float, float], p: Point, q: Point
) -> bool:
    lo_x, hi_x = min(p[0], q[0]), max(p[0], q[0])
    lo_y, hi_y = min(p[1], q[1]), max(p[1], q[1])
    return lo_x < r[2] and hi_x > r[0] and lo_y < r[3] and hi_y > r[1]


def _place_labels(
    labelled: list[tuple[int, str, list[Point]]],
    boxes: dict[str, _Box],
    all_paths: list[list[Point]],
) -> dict[int, tuple[float, float, bool]]:
    """Top-left of each label: the first free spot beside its first segment.

    Candidates are tried in preference order (the usual textbook position
    first); each is scored by collisions with nodes, every routed line and
    labels already placed, so a label never sits on a line or a symbol
    unless there is literally no free candidate.
    """
    placed: list[tuple[float, float, float, float]] = []
    result: dict[int, tuple[float, float, bool]] = {}
    node_rects = [
        (b.x - 2, b.y - 2, b.x + b.w + 2, b.y + b.h + 2) for b in boxes.values()
    ]
    segs = [(p, q) for path in all_paths for p, q in zip(path, path[1:], strict=False)]
    for idx, label, path in labelled:
        (x0, y0), (x1, y1) = path[0], path[1]
        tw = len(label) * _LABEL_CHAR_PX + 4
        seg_len = abs(x1 - x0) + abs(y1 - y0)
        # (x, y, right_aligned): right-aligned labels sit left of the line.
        cands: list[tuple[float, float, bool]] = []
        for along in (4.0, 16.0, 32.0, seg_len / 2):
            if along > seg_len:
                continue
            if abs(x1 - x0) < 0.01:  # vertical first segment
                down = 1 if y1 > y0 else -1
                yy = y0 + down * along - (0 if down > 0 else _LABEL_H)
                cands += [(x0 + 6, yy, False), (x0 - 6 - tw, yy, True)]
            else:
                right = 1 if x1 > x0 else -1
                xx = x0 + right * along - (0 if right > 0 else tw)
                cands += [(xx, y0 - _LABEL_H - 2, right < 0), (xx, y0 + 3, right < 0)]
        best: tuple[float, tuple[float, float, bool]] | None = None
        for order, (lx, ly, align_r) in enumerate(cands):
            rect = (lx, ly, lx + tw, ly + _LABEL_H)
            score = float(order)
            score += 1000 * sum(_rect_hits_segment(rect, *s) for s in segs)
            score += 1000 * sum(
                rect[0] < r[2] and rect[2] > r[0] and rect[1] < r[3] and rect[3] > r[1]
                for r in node_rects + placed
            )
            if best is None or score < best[0]:
                best = (score, (lx, ly, align_r))
        assert best is not None
        lx, ly, align_r = best[1]
        placed.append((lx, ly, lx + tw, ly + _LABEL_H))
        if align_r:
            # Renderers draw a fixed-width box; anchor its RIGHT edge.
            lx = lx + tw - _RENDER_LABEL_W
        result[idx] = (lx, ly, align_r)
    return result


def _rip_up_conflicts(
    router: _Router,
    edges: list[DiagramEdge],
    routed: dict[int, tuple[list[Point], bool]],
    ports: dict[int, tuple[_Port, _Port]],
) -> None:
    """Re-route edges that cross or overlap an unrelated edge.

    Edges are routed one by one, so an early edge cannot see the ones
    routed after it. Each round tries re-routing every conflicting edge
    with all others in place and applies the single best fix: the largest
    drop in conflicts, then the smallest extra cost — so the edge that can
    move cheaply (e.g. a long line into "Son") moves, not the decision
    branch that would have to leave on the wrong side.
    """

    def others(i: int) -> list[list[Point]]:
        return [
            path
            for j, (path, _) in routed.items()
            if j != i and edges[j].target_id != edges[i].target_id
        ]

    for _ in range(_RIPUP_ROUNDS):
        best: tuple[int, float, int, tuple[list[Point], _Port, _Port]] | None = None
        for i in sorted(ports):
            path, back = routed[i]
            before = _conflicts(path, others(i))
            if not before:
                continue
            s_port, t_port = ports[i]
            router.uncommit(i, s_port, t_port)
            old_cost = router.path_cost(edges[i], path, s_port, t_port, back)
            result = router.route(edges[i], back)
            extra = router.last_cost - old_cost
            router.commit(edges[i], path, s_port, t_port, key=i)
            if result is None:
                continue
            gain = before - _conflicts(result[0], others(i))
            if gain > 0 and (best is None or (-gain, extra) < (-best[0], best[1])):
                best = (gain, extra, i, result)
        if best is None:
            return
        _, _, i, (path, s_port, t_port) = best
        router.uncommit(i, *ports[i])
        router.commit(edges[i], path, s_port, t_port, key=i)
        routed[i] = (path, routed[i][1])
        ports[i] = (s_port, t_port)


def layout_from_boxes(graph: DiagramGraph) -> LayoutedGraph | None:
    """Lay out ``graph`` using the source image's own node positions.

    Returns ``None`` when node boxes are missing/invalid so the caller can
    fall back to the Graphviz layout.
    """
    if not graph.nodes:
        return None
    placed = _place_nodes(graph)
    if placed is None:
        return None
    boxes, guides = placed

    diamonds = {n.id for n in graph.nodes if n.shape_type.value == "decision"}
    documents = {n.id for n in graph.nodes if n.shape_type.value == "document"}
    router = _Router(boxes, guides, diamonds, documents)

    def is_back(e: DiagramEdge) -> bool:
        s, t = boxes[e.source_id], boxes[e.target_id]
        return e.source_id != e.target_id and t.y + t.h <= s.y + 1.0

    known = set(boxes)
    valid = [e for e in graph.edges if e.source_id in known and e.target_id in known]
    order = sorted(range(len(valid)), key=lambda i: (is_back(valid[i]), i))

    routed: dict[int, tuple[list[Point], bool]] = {}
    ports: dict[int, tuple[_Port, _Port]] = {}
    for i in order:
        e = valid[i]
        back = is_back(e)
        if e.source_id == e.target_id:
            routed[i] = (_self_loop(boxes[e.source_id]), True)
            continue
        result = router.route(e, back)
        if result is None:
            logger.warning("No route for edge %s->%s", e.source_id, e.target_id)
            continue
        path, s_side, t_side = result
        router.commit(e, path, s_side, t_side, key=i)
        routed[i] = (path, back)
        ports[i] = (s_side, t_side)
    _rip_up_conflicts(router, valid, routed, ports)
    _cut_at_merges(valid, routed)

    labels = _place_labels(
        [
            (i, valid[i].label or "", routed[i][0])
            for i in sorted(routed)
            if valid[i].label
        ],
        boxes,
        [p for p, _ in routed.values()],
    )

    # Shift so everything (nodes + routes + labels) sits inside the margin.
    xs = [b.x for b in boxes.values()] + [v[0] for v in labels.values()]
    ys = [b.y for b in boxes.values()] + [v[1] for v in labels.values()]
    for path, _ in routed.values():
        xs += [p[0] for p in path]
        ys += [p[1] for p in path]
    dx, dy = _MARGIN - min(xs), _MARGIN - min(ys)

    nodes = [
        LayoutedNode(
            **n.model_dump(exclude={"box_2d", "mark"}),
            x=boxes[n.id].x + dx,
            y=boxes[n.id].y + dy,
            width=boxes[n.id].w,
            height=boxes[n.id].h,
        )
        for n in graph.nodes
    ]
    edges: list[DiagramEdge] = []
    for i, e in enumerate(valid):
        if i not in routed:
            edges.append(e)
            continue
        path, back = routed[i]
        lx = ly = None
        align_r = False
        if i in labels:
            lx, ly, align_r = labels[i][0] + dx, labels[i][1] + dy, labels[i][2]
        edges.append(
            e.model_copy(
                update={
                    "edge_path": [[p[0] + dx, p[1] + dy] for p in path],
                    "exact_route": True,
                    "is_back_edge": back,
                    "label_x": lx,
                    "label_y": ly,
                    "label_align_right": align_r,
                }
            )
        )
    logger.info(
        "Box layout (%s boxes): %d nodes, %d edges (%d routed), %d guides",
        "detected" if graph.boxes_detected else "model",
        len(nodes),
        len(edges),
        len(routed),
        len(guides),
    )
    return LayoutedGraph(nodes=nodes, edges=edges)
