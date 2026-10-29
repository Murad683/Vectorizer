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

from app.core.shape_geometry import DOCUMENT_TEXT_BOTTOM, document_bottom
from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    LayoutedGraph,
    LayoutedNode,
    PortSide,
)

logger = logging.getLogger(__name__)

_TARGET_NODE_WIDTH = 150.0
_NODE_GAP_APPROX = 40.0
_NODE_GAP_EXACT = 26.0
_STUB = 12.0
_INFLATE = 4.0
_BEND_COST = 100.0
_OVERLAP_COST_PER_PX = 3.0
_CROSS_COST = 150.0
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


def _min_size(text: str, shape: str) -> tuple[float, float]:
    """Smallest comfortable size for the node's text at 12 pt."""
    base_w, base_h = _SHAPE_MIN[shape]
    lines = text.split("\n")
    needed = max(len(line) for line in lines) * 8.0 + 30.0
    height = base_h + (len(lines) - 1) * 16.0
    if shape == "decision":
        needed *= 1.6  # text must fit inside the diamond's inner area
        height += (len(lines) - 1) * 16.0
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


def _place_nodes(
    graph: DiagramGraph,
) -> tuple[dict[str, _Box], list[Segment]] | None:
    """Scale/snap/spread the source boxes and guide lines.

    Returns ``(boxes, guides)`` in layout px, or None if boxes are unusable.
    """
    width = float(graph.image_width or 1000)
    height = float(graph.image_height or 1000)
    exact = graph.boxes_detected
    raw: dict[str, tuple[float, float, float, float]] = {}
    for node in graph.nodes:
        b = node.box_2d
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

    median_w = statistics.median(v[2] for v in raw.values())
    scale = _TARGET_NODE_WIDTH / median_w
    if exact:
        # One uniform scale large enough that every node's text fits in
        # its ORIGINAL proportions (no per-node growth → nothing collides).
        need = max(
            max(
                _min_size(n.text, n.shape_type.value)[0] / raw[n.id][2],
                _min_size(n.text, n.shape_type.value)[1] / raw[n.id][3],
            )
            for n in graph.nodes
        )
        scale = min(max(scale, need), scale * _MAX_TEXT_UPSCALE)

    ids = [n.id for n in graph.nodes]
    cxs, cys, ws, hs = [], [], [], []
    for node in graph.nodes:
        x, y, w, h = raw[node.id]
        mw, mh = _min_size(node.text, node.shape_type.value)
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
    new_cxs = _cluster(cxs, tol)
    new_cys = _cluster(cys, tol * (1.0 if exact else 0.6))

    def follow(v: float, old: list[float], new: list[float]) -> float:
        # A guide moves with the node centre it was aligned with; guides
        # are never merged with each other (distinct parallel lines stay
        # distinct).
        best = min(range(len(old)), key=lambda i: abs(old[i] - v))
        return v + (new[best] - old[best]) if abs(old[best] - v) <= tol else v

    vert = [(follow(v[0], cxs, new_cxs), v[1], v[2]) for v in vert]
    horiz = [(follow(h[0], cys, new_cys), h[1], h[2]) for h in horiz]
    cxs, cys = new_cxs, new_cys

    boxes: dict[str, _Box] = {}
    gap = _NODE_GAP_EXACT if exact else _NODE_GAP_APPROX
    mx, my = sum(cxs) / len(cxs), sum(cys) / len(cys)
    spread = 1.0
    for _ in range(20):
        for i, nid in enumerate(ids):
            cx = mx + (cxs[i] - mx) * spread
            cy = my + (cys[i] - my) * spread
            boxes[nid] = _Box(cx - ws[i] / 2, cy - hs[i] / 2, ws[i], hs[i])
        items = list(boxes.values())
        if not any(
            _too_close(items[i], items[j], gap)
            for i in range(len(items))
            for j in range(i + 1, len(items))
        ):
            break
        spread *= 1.12

    def sx(v: float) -> float:
        return mx + (v - mx) * spread

    def sy(v: float) -> float:
        return my + (v - my) * spread

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
        # Segments already used by routed edges: (x0, y0, x1, y1, target_id),
        # x0<=x1, y0<=y1.
        self.used: list[tuple[float, float, float, float, str]] = []
        self.port_use: dict[tuple[str, float, float], dict[str, int]] = {}
        self.side_out: dict[tuple[str, PortSide], int] = {}

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
        for ux0, uy0, ux1, uy1, u_target in self.used:
            if u_target == target:
                continue  # edges into the same node may merge freely
            u_vertical = abs(ux0 - ux1) < 0.01
            if vertical and u_vertical:
                if abs(ux0 - p[0]) < 1.0:
                    cost += _OVERLAP_COST_PER_PX * max(
                        0.0, min(hi_y, uy1) - max(lo_y, uy0)
                    )
            elif not vertical and not u_vertical:
                if abs(uy0 - p[1]) < 1.0:
                    cost += _OVERLAP_COST_PER_PX * max(
                        0.0, min(hi_x, ux1) - max(lo_x, ux0)
                    )
            elif vertical:  # p-q vertical, u horizontal
                if lo_y < uy0 < hi_y and ux0 < p[0] < ux1:
                    cost += _CROSS_COST
            else:
                if lo_x < ux0 < hi_x and uy0 < p[1] < uy1:
                    cost += _CROSS_COST
        return cost

    def _step_cost(self, p: Point, q: Point, target: str) -> float:
        length = abs(q[0] - p[0]) + abs(q[1] - p[1])
        if self._on_guide(p, q):
            length *= _GUIDE_FACTOR
        return length + self._penalty(p, q, target)

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
            pen += 120.0
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

    def commit(self, edge: DiagramEdge, path: list[Point], s: _Port, t: _Port) -> None:
        """Record a routed edge so later edges avoid/penalise it."""
        for p, q in zip(path, path[1:], strict=False):
            self.used.append(
                (
                    min(p[0], q[0]),
                    min(p[1], q[1]),
                    max(p[0], q[0]),
                    max(p[1], q[1]),
                    edge.target_id,
                )
            )
        self.port_use.setdefault(s.key, {}).setdefault("out", 0)
        self.port_use[s.key]["out"] += 1
        self.port_use.setdefault(t.key, {}).setdefault("in", 0)
        self.port_use[t.key]["in"] += 1
        self.side_out[(s.node, s.side)] = self.side_out.get((s.node, s.side), 0) + 1


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
        router.commit(e, path, s_side, t_side)
        routed[i] = (path, back)

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
