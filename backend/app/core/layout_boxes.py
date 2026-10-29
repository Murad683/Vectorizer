"""Layout that preserves the source image's own arrangement.

The vision model reports each symbol's bounding box (``box_2d``). Instead
of re-flowing the diagram with Graphviz (which ignores the original
shape, e.g. loops and side branches), this module keeps the nodes where
they were in the image — scaled, snapped into aligned rows/columns and
spread until nothing touches — and then routes every edge as an
orthogonal polyline with a small A* router over a Hanan-style grid.

No external binaries and no API calls are involved. ``layout_from_boxes``
returns ``None`` when the graph lacks usable boxes; the caller then falls
back to the Graphviz layout.
"""

import heapq
import logging
import statistics
from dataclasses import dataclass

from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    LayoutedGraph,
    LayoutedNode,
    PortSide,
)

logger = logging.getLogger(__name__)

_TARGET_NODE_WIDTH = 150.0
_NODE_GAP = 40.0
_STUB = 20.0
_INFLATE = 4.0
_BEND_COST = 100.0
_OVERLAP_COST_PER_PX = 3.0
_CROSS_COST = 90.0
_MARGIN = 20.0
_LABEL_CHAR_PX = 8.0
_LABEL_H = 22.0
_SNAP_RATIO = 0.2

# N, E, S, W as (dx, dy); layout px have a top-left origin.
_DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]
_SIDE_NORMAL = {
    PortSide.TOP: (0, -1),
    PortSide.RIGHT: (1, 0),
    PortSide.BOTTOM: (0, 1),
    PortSide.LEFT: (-1, 0),
}
Point = tuple[float, float]


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


def _min_size(node_text: str, shape_min: tuple[float, float]) -> tuple[float, float]:
    """Smallest size that still fits the node's text."""
    needed = len(node_text) * 8.0 + 30.0
    return max(shape_min[0], needed), shape_min[1]


def _shape_minimums() -> dict[str, tuple[float, float]]:
    return {
        "terminator": (120.0, 44.0),
        "process": (120.0, 44.0),
        "input_output": (130.0, 44.0),
        "decision": (150.0, 80.0),
    }


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


def _too_close(a: _Box, b: _Box) -> bool:
    gx = max(a.x - (b.x + b.w), b.x - (a.x + a.w))
    gy = max(a.y - (b.y + b.h), b.y - (a.y + a.h))
    return gx < _NODE_GAP and gy < _NODE_GAP


def _place_nodes(graph: DiagramGraph) -> dict[str, _Box] | None:
    """Scale/snap/spread the source boxes; None if boxes are unusable."""
    width = float(graph.image_width or 1000)
    height = float(graph.image_height or 1000)
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
    if len({node.id for node in graph.nodes}) != len(graph.nodes):
        return None

    median_w = statistics.median(v[2] for v in raw.values())
    scale = _TARGET_NODE_WIDTH / median_w
    mins = _shape_minimums()

    ids = [n.id for n in graph.nodes]
    cxs, cys, ws, hs = [], [], [], []
    for node in graph.nodes:
        x, y, w, h = raw[node.id]
        mw, mh = _min_size(node.text, mins[node.shape_type.value])
        ws.append(max(w * scale, mw))
        hs.append(max(h * scale, mh))
        cxs.append((x + w / 2) * scale)
        cys.append((y + h / 2) * scale)

    tol = _SNAP_RATIO * _TARGET_NODE_WIDTH
    cxs = _cluster(cxs, tol)
    cys = _cluster(cys, tol * 0.6)

    boxes: dict[str, _Box] = {}

    def build(spread: float) -> None:
        mx, my = sum(cxs) / len(cxs), sum(cys) / len(cys)
        for i, nid in enumerate(ids):
            cx = mx + (cxs[i] - mx) * spread
            cy = my + (cys[i] - my) * spread
            boxes[nid] = _Box(cx - ws[i] / 2, cy - hs[i] / 2, ws[i], hs[i])

    spread = 1.0
    for _ in range(20):
        build(spread)
        items = list(boxes.values())
        if not any(
            _too_close(items[i], items[j])
            for i in range(len(items))
            for j in range(i + 1, len(items))
        ):
            break
        spread *= 1.12
    return boxes


class _Router:
    """A* orthogonal edge router over a grid built from the node boxes."""

    def __init__(self, boxes: dict[str, _Box]) -> None:
        self.boxes = boxes
        self.obstacles = {
            nid: (
                b.x - _INFLATE,
                b.y - _INFLATE,
                b.x + b.w + _INFLATE,
                b.y + b.h + _INFLATE,
            )
            for nid, b in boxes.items()
        }
        xs: set[float] = set()
        ys: set[float] = set()
        for b in boxes.values():
            for dx in (-12.0, -_STUB, 0.0, b.w + 12.0, b.w + _STUB):
                xs.add(round(b.x + dx, 2))
            xs.add(round(b.cx, 2))
            for dy in (-12.0, -_STUB, 0.0, b.h + 12.0, b.h + _STUB):
                ys.add(round(b.y + dy, 2))
            ys.add(round(b.cy, 2))
        xs.add(round(min(b.x for b in boxes.values()) - 3 * _STUB, 2))
        xs.add(round(max(b.x + b.w for b in boxes.values()) + 3 * _STUB, 2))
        ys.add(round(min(b.y for b in boxes.values()) - 3 * _STUB, 2))
        ys.add(round(max(b.y + b.h for b in boxes.values()) + 3 * _STUB, 2))
        self.xs = sorted(xs)
        self.ys = sorted(ys)
        self.xi = {v: i for i, v in enumerate(self.xs)}
        self.yi = {v: i for i, v in enumerate(self.ys)}
        # Segments already used by routed edges: (x0, y0, x1, y1, target_id), x0<=x1, y0<=y1.
        self.used: list[tuple[float, float, float, float, str]] = []
        self.port_use: dict[tuple[str, PortSide], dict[str, int]] = {}

    # -- geometry helpers -------------------------------------------------
    def _clear(self, p: Point, q: Point) -> bool:
        """True if the axis-aligned segment avoids every inflated node."""
        lo_x, hi_x = min(p[0], q[0]), max(p[0], q[0])
        lo_y, hi_y = min(p[1], q[1]), max(p[1], q[1])
        for x0, y0, x1, y1 in self.obstacles.values():
            if lo_x < x1 and hi_x > x0 and lo_y < y1 and hi_y > y0:
                return False
        return True

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

    def _port_penalty(
        self, nid: str, side: PortSide, role: str, hint: PortSide | None,
        toward: tuple[float, float] | None,
    ) -> float:
        pen = 0.0
        normal = _SIDE_NORMAL[side]
        if hint is not None and side != hint:
            pen += 120.0
        if toward is not None and hint is None:
            if normal[0] * toward[0] + normal[1] * toward[1] <= 0:
                pen += 80.0
        use = self.port_use.get((nid, side), {})
        other = "in" if role == "out" else "out"
        if use.get(other):
            pen += 300.0
        if use.get(role):
            pen += 300.0 if role == "out" else 40.0
        return pen

    # -- routing ----------------------------------------------------------
    def route(
        self,
        edge: DiagramEdge,
        is_back: bool,
    ) -> tuple[list[Point], PortSide, PortSide] | None:
        """Route one edge; returns (polyline, source_side, target_side)."""
        sb, tb = self.boxes[edge.source_id], self.boxes[edge.target_id]
        to_target = (tb.cx - sb.cx, tb.cy - sb.cy)
        to_source = (-to_target[0], -to_target[1])
        sides = list(PortSide)

        starts: list[tuple[float, Point, int, PortSide]] = []
        for side in sides:
            port = sb.port(side)
            n = _SIDE_NORMAL[side]
            stub = (round(port[0] + n[0] * _STUB, 2), round(port[1] + n[1] * _STUB, 2))
            if stub[0] not in self.xi or stub[1] not in self.yi:
                continue
            if not self._clear(stub, stub):
                continue
            pen = self._port_penalty(
                edge.source_id, side, "out", edge.source_port,
                None if is_back else to_target,
            )
            starts.append((pen + _STUB, stub, _DIRS.index(n), side))

        goals: dict[Point, tuple[float, PortSide, int]] = {}
        for side in sides:
            port = tb.port(side)
            n = _SIDE_NORMAL[side]
            stub = (round(port[0] + n[0] * _STUB, 2), round(port[1] + n[1] * _STUB, 2))
            pen = self._port_penalty(
                edge.target_id, side, "in", None,
                None if is_back else to_source,
            )
            # Arriving moves INTO the port, i.e. against the outward normal.
            goals[stub] = (pen + _STUB, side, _DIRS.index((-n[0], -n[1])))

        goal_pts = list(goals)

        def h(p: Point) -> float:
            return min(abs(p[0] - g[0]) + abs(p[1] - g[1]) for g in goal_pts)

        State = tuple[int, int, int]
        dist: dict[State, float] = {}
        parent: dict[State, State | None] = {}
        start_side: dict[State, PortSide] = {}
        heap: list[tuple[float, float, State]] = []
        for pen, stub, d, side in starts:
            st = (self.xi[stub[0]], self.yi[stub[1]], d)
            if pen < dist.get(st, float("inf")):
                dist[st] = pen
                parent[st] = None
                start_side[st] = side
                heapq.heappush(heap, (pen + h(stub), pen, st))

        best: tuple[float, State, PortSide] | None = None
        while heap:
            f, g, st = heapq.heappop(heap)
            if best is not None and f >= best[0]:
                break
            if g > dist.get(st, float("inf")):
                continue
            ix, iy, d = st
            here = (self.xs[ix], self.ys[iy])
            if here in goals:
                gpen, gside, inward = goals[here]
                total = g + gpen + (0.0 if d == inward else _BEND_COST)
                if best is None or total < best[0]:
                    best = (total, st, gside)
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
                step = abs(there[0] - here[0]) + abs(there[1] - here[1])
                step += self._penalty(here, there, edge.target_id)
                if nd != d:
                    step += _BEND_COST
                ng = g + step
                nst = (jx, jy, nd)
                if ng < dist.get(nst, float("inf")):
                    dist[nst] = ng
                    parent[nst] = st
                    start_side[nst] = start_side[st]
                    heapq.heappush(heap, (ng + h(there), ng, nst))

        if best is None:
            return None
        _, end_state, tside = best
        pts: list[Point] = []
        cur: State | None = end_state
        while cur is not None:
            pts.append((self.xs[cur[0]], self.ys[cur[1]]))
            cur = parent[cur]
        pts.reverse()
        sside = start_side[end_state]
        path = [sb.port(sside), *pts, tb.port(tside)]
        return _simplify(path), sside, tside

    def commit(self, edge: DiagramEdge, path: list[Point], s: PortSide, t: PortSide) -> None:
        """Record a routed edge so later edges avoid/penalise it."""
        for p, q in zip(path, path[1:]):
            self.used.append(
                (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]), edge.target_id)
            )
        self.port_use.setdefault((edge.source_id, s), {}).setdefault("out", 0)
        self.port_use[(edge.source_id, s)]["out"] += 1
        self.port_use.setdefault((edge.target_id, t), {}).setdefault("in", 0)
        self.port_use[(edge.target_id, t)]["in"] += 1


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


def _label_position(
    label: str, path: list[Point]
) -> tuple[float, float]:
    """Top-left of the label box, next to the edge's first segment."""
    (x0, y0), (x1, y1) = path[0], path[1]
    text_w = len(label) * _LABEL_CHAR_PX + 4
    if abs(x1 - x0) < 0.01:  # vertical start
        down = y1 > y0
        return x0 + 6, (y0 + 2) if down else (y0 - _LABEL_H)
    right = x1 > x0
    return ((x0 + 6) if right else (x0 - 6 - text_w)), y0 - _LABEL_H + 2


def layout_from_boxes(graph: DiagramGraph) -> LayoutedGraph | None:
    """Lay out ``graph`` using the source image's own node positions.

    Returns ``None`` when node boxes are missing/invalid so the caller can
    fall back to the Graphviz layout.
    """
    if not graph.nodes:
        return None
    boxes = _place_nodes(graph)
    if boxes is None:
        return None

    router = _Router(boxes)
    centers = {nid: b.cy for nid, b in boxes.items()}

    def is_back(e: DiagramEdge) -> bool:
        return (
            e.source_id != e.target_id
            and centers[e.target_id] < centers[e.source_id] - 1.0
            and boxes[e.target_id].y + boxes[e.target_id].h <= boxes[e.source_id].y + 1.0
        )

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

    # Shift so everything (nodes + routes) sits inside the margin.
    xs = [b.x for b in boxes.values()]
    ys = [b.y for b in boxes.values()]
    for path, _ in routed.values():
        xs += [p[0] for p in path]
        ys += [p[1] for p in path]
    dx, dy = _MARGIN - min(xs), _MARGIN - min(ys)

    nodes = [
        LayoutedNode(
            **n.model_dump(exclude={"box_2d"}),
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
        shifted = [[p[0] + dx, p[1] + dy] for p in path]
        lx = ly = None
        if e.label:
            lx, ly = _label_position(e.label, [(p[0], p[1]) for p in shifted])
        edges.append(
            e.model_copy(
                update={
                    "edge_path": shifted,
                    "exact_route": True,
                    "is_back_edge": back,
                    "label_x": lx,
                    "label_y": ly,
                }
            )
        )
    logger.info(
        "Box layout: %d nodes, %d edges (%d routed)",
        len(nodes), len(edges), len(routed),
    )
    return LayoutedGraph(nodes=nodes, edges=edges)
