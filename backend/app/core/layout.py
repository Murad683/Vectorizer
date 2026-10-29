"""Graphviz-based layout engine for flowchart graphs.

Turns a topological ``DiagramGraph`` into a ``LayoutedGraph`` with
concrete x/y/width/height per node and label positions per edge,
using Graphviz's ``dot`` algorithm for hierarchical directed-graph
layout with ``splines=ortho`` for axis-aligned edge routing.

Graphviz handles node placement, edge routing, label positioning,
collision avoidance, and cycle handling (back-edge ranking). The
renderers (``pptx_shapes.py``, ``docx_shapes.py``) consume the
positioned coordinates and edge waypoints to create native OOXML
shapes with properly-routed, glued connectors.
"""

import json
import logging
import os
import re

import graphviz

from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    DiagramNode,
    LayoutedGraph,
    LayoutedNode,
    PortSide,
    ShapeType,
)

logger = logging.getLogger(__name__)

_SHAPE_MAP: dict[ShapeType, str] = {
    ShapeType.TERMINATOR: "oval",
    ShapeType.PROCESS: "rectangle",
    ShapeType.DECISION: "diamond",
    ShapeType.INPUT_OUTPUT: "parallelogram",
    ShapeType.DOCUMENT: "rectangle",
}

# Graphviz uses 72 points per inch; layout px are 96 dpi (1 px = 9525 EMU).
_GV_DPI = 72.0
_LAYOUT_DPI = 96.0
_GV_TO_PX = _LAYOUT_DPI / _GV_DPI

_MARGIN_PX = 20.0
_LABEL_HEIGHT_PX = 22.0
_LABEL_WIDTH_PX = 60.0


def _node_size_inches(node: DiagramNode) -> tuple[float, float]:
    """Minimum node size in inches for Graphviz, based on text length."""
    char_width = 8.0 / _LAYOUT_DPI
    text_pad = 30.0 / _LAYOUT_DPI
    defaults: dict[ShapeType, tuple[float, float]] = {
        ShapeType.PROCESS: (160.0 / _LAYOUT_DPI, 50.0 / _LAYOUT_DPI),
        ShapeType.INPUT_OUTPUT: (160.0 / _LAYOUT_DPI, 50.0 / _LAYOUT_DPI),
        ShapeType.DOCUMENT: (160.0 / _LAYOUT_DPI, 56.0 / _LAYOUT_DPI),
        ShapeType.DECISION: (180.0 / _LAYOUT_DPI, 90.0 / _LAYOUT_DPI),
        ShapeType.TERMINATOR: (140.0 / _LAYOUT_DPI, 50.0 / _LAYOUT_DPI),
    }
    w_default, h_default = defaults[node.shape_type]
    lines = node.text.split("\n")
    w_needed = max(len(line) for line in lines) * char_width + text_pad
    h_needed = h_default + (len(lines) - 1) * 16.0 / _LAYOUT_DPI
    return max(w_default, w_needed), h_needed


def _parse_edge_pos(pos_str: str) -> tuple[list[list[float]], list[float] | None]:
    """Parse a Graphviz edge ``pos`` string into waypoints.

    Returns ``(control_points, endpoint)`` where ``endpoint`` is the
    arrowhead position (from the ``e,x,y`` prefix) if present.

    The ``pos`` format is:
    ``[s,sx,sy] [e,ex,ey] p1x,p1y p2x,p2y ...``
    where the remaining points are B-spline control points in groups of
    3 (cubic Bezier segments).
    """
    endpoint: list[float] | None = None
    remaining = pos_str.strip()

    # Extract optional s,x,y and e,x,y prefixes.
    while True:
        m = re.match(r"([se]),([^,]+),(\S+)\s*", remaining)
        if not m:
            break
        prefix_type = m.group(1)
        px, py = float(m.group(2)), float(m.group(3))
        if prefix_type == "e":
            endpoint = [px, py]
        remaining = remaining[m.end() :]

    # Parse remaining control points.
    points: list[list[float]] = []
    for pair in remaining.split():
        parts = pair.split(",")
        if len(parts) == 2:
            points.append([float(parts[0]), float(parts[1])])

    return points, endpoint


def _simplify_ortho_path(
    control_points: list[list[float]], endpoint: list[float] | None
) -> list[list[float]]:
    """Extract meaningful waypoints from B-spline control points.

    For ortho splines, each cubic Bezier segment is a straight
    axis-aligned line. We take the start point and every 3rd point
    (segment endpoints), append the arrowhead endpoint, deduplicate
    consecutive identical points, and remove collinear interior points.
    """
    if not control_points:
        return []

    # Extract segment endpoints: first point + every 3rd after.
    key_points: list[list[float]] = [control_points[0]]
    i = 3
    while i < len(control_points):
        key_points.append(control_points[i])
        i += 3
    if len(control_points) > 1 and len(key_points) == 1:
        key_points.append(control_points[-1])

    if endpoint is not None:
        key_points.append(endpoint)

    # Deduplicate consecutive identical points (within tolerance).
    deduped: list[list[float]] = [key_points[0]]
    for pt in key_points[1:]:
        if abs(pt[0] - deduped[-1][0]) > 0.5 or abs(pt[1] - deduped[-1][1]) > 0.5:
            deduped.append(pt)

    # Remove collinear interior points.
    if len(deduped) <= 2:
        return deduped
    result: list[list[float]] = [deduped[0]]
    for j in range(1, len(deduped) - 1):
        prev, cur, nxt = deduped[j - 1], deduped[j], deduped[j + 1]
        same_x = abs(prev[0] - cur[0]) < 0.5 and abs(cur[0] - nxt[0]) < 0.5
        same_y = abs(prev[1] - cur[1]) < 0.5 and abs(cur[1] - nxt[1]) < 0.5
        if not (same_x or same_y):
            result.append(cur)
    result.append(deduped[-1])
    return result


def _validate_source_ports(
    edges: list[DiagramEdge],
) -> dict[int, PortSide]:
    """Return a mapping from edge object id to validated PortSide.

    If two edges from the same source node claim the same port, all
    port hints for that node are dropped (graceful degradation).
    """
    by_node: dict[str, list[DiagramEdge]] = {}
    for e in edges:
        if e.source_port is not None:
            by_node.setdefault(e.source_id, []).append(e)

    result: dict[int, PortSide] = {}
    for node_id, node_edges in by_node.items():
        ports_used: dict[PortSide, int] = {}
        for e in node_edges:
            assert e.source_port is not None
            ports_used[e.source_port] = ports_used.get(e.source_port, 0) + 1

        has_conflict = any(c > 1 for c in ports_used.values())
        if has_conflict:
            logger.warning(
                "Conflicting source_port hints for node %s; ignoring all",
                node_id,
            )
            continue

        for e in node_edges:
            assert e.source_port is not None
            result[id(e)] = e.source_port

    return result


def layout_graph(graph: DiagramGraph) -> LayoutedGraph:
    """Compute geometry for every node of ``graph`` using Graphviz dot.

    Returns a ``LayoutedGraph`` whose edges carry ``is_back_edge``,
    ``label_x``/``label_y``, and ``edge_path`` fields set by the layout
    engine.
    """
    if not graph.nodes:
        return LayoutedGraph(nodes=[], edges=[])

    # Prefer the source image's own arrangement when the vision model
    # reported usable node boxes; Graphviz is the fallback.
    from app.core.layout_boxes import layout_from_boxes

    boxed = layout_from_boxes(graph)
    if boxed is not None:
        return boxed

    # Debug: dump incoming graph for diagnosis
    for n in graph.nodes:
        logger.info("GRAPH NODE %s: %s '%s'", n.id, n.shape_type, n.text)
    for e in graph.edges:
        logger.info(
            "GRAPH EDGE %s->%s label=%r source_port=%s",
            e.source_id, e.target_id, e.label, e.source_port,
        )

    g = graphviz.Digraph(format="json0")
    g.attr(rankdir="TB", splines="ortho", nodesep="0.5", ranksep="0.8")
    g.attr("node", fontsize="12", style="filled", fillcolor="white")

    node_lookup: dict[str, DiagramNode] = {}
    for node in graph.nodes:
        node_lookup[node.id] = node
        w_in, h_in = _node_size_inches(node)
        g.node(
            node.id,
            label=node.text,
            shape=_SHAPE_MAP[node.shape_type],
            width=str(w_in),
            height=str(h_in),
            fixedsize="true",
        )

    # Validate source_port hints: clear all ports for a node if two
    # edges claim the same port (conflict).
    _port_map = {"top": "n", "bottom": "s", "left": "w", "right": "e"}
    _validated_ports = _validate_source_ports(graph.edges)

    same_rank_pairs: list[tuple[str, str]] = []
    for edge in graph.edges:
        attrs: dict[str, str] = {}
        if edge.label:
            attrs["xlabel"] = edge.label
        src = edge.source_id
        port = _validated_ports.get(id(edge))
        if port:
            src = f"{edge.source_id}:{_port_map[port.value]}"
            if port in (PortSide.LEFT, PortSide.RIGHT):
                same_rank_pairs.append((edge.source_id, edge.target_id))
                attrs["constraint"] = "false"
        g.edge(src, edge.target_id, **attrs)

    for src_id, tgt_id in same_rank_pairs:
        g.body.append(f'  {{ rank=same; "{src_id}"; "{tgt_id}"; }}')

    # Ensure Graphviz binaries are on PATH.
    gv_bin = os.environ.get("GRAPHVIZ_BIN", r"C:\Program Files\Graphviz\bin")
    if os.path.isdir(gv_bin) and gv_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + gv_bin

    raw = g.pipe(engine="dot", format="json0")
    data = json.loads(raw)

    bb = data.get("bb", "0,0,100,100")
    bb_parts = [float(x) for x in bb.split(",")]
    bb_max_y = bb_parts[3]

    def gv_to_px(gv_x: float, gv_y: float) -> tuple[float, float]:
        px_x = gv_x * _GV_TO_PX + _MARGIN_PX
        px_y = (bb_max_y - gv_y) * _GV_TO_PX + _MARGIN_PX
        return px_x, px_y

    gv_nodes: dict[str, dict] = {}
    for obj in data.get("objects", []):
        gv_nodes[obj["name"]] = obj

    layouted_nodes: list[LayoutedNode] = []
    node_positions: dict[str, tuple[float, float]] = {}
    for node in graph.nodes:
        gv = gv_nodes[node.id]
        cx_gv, cy_gv = [float(v) for v in gv["pos"].split(",")]
        w_in = float(gv["width"])
        h_in = float(gv["height"])
        w_px = w_in * _LAYOUT_DPI
        h_px = h_in * _LAYOUT_DPI
        cx_px, cy_px = gv_to_px(cx_gv, cy_gv)
        x = cx_px - w_px / 2
        y = cy_px - h_px / 2
        node_positions[node.id] = (cy_px, y)
        layouted_nodes.append(
            LayoutedNode(
                **node.model_dump(),
                x=x,
                y=y,
                width=w_px,
                height=h_px,
            )
        )

    gv_node_indices: dict[str, int] = {}
    for obj in data.get("objects", []):
        gv_node_indices[obj["name"]] = obj["_gvid"]
    gv_id_to_name: dict[int, str] = {v: k for k, v in gv_node_indices.items()}

    layouted_edges: list[DiagramEdge] = []
    for gv_edge in data.get("edges", []):
        tail_id = gv_id_to_name[gv_edge["tail"]]
        head_id = gv_id_to_name[gv_edge["head"]]
        input_edge = _find_input_edge(graph.edges, tail_id, head_id)
        if input_edge is None:
            continue

        source_cy = node_positions[tail_id][0]
        target_cy = node_positions[head_id][0]
        is_back = target_cy < source_cy and tail_id != head_id

        label_x: float | None = None
        label_y: float | None = None
        lp_key = "xlp" if "xlp" in gv_edge else "lp"
        if input_edge.label and lp_key in gv_edge:
            lp_x, lp_y = [float(v) for v in gv_edge[lp_key].split(",")]
            lbl_cx, lbl_cy = gv_to_px(lp_x, lp_y)
            label_x = lbl_cx - _LABEL_WIDTH_PX / 2
            label_y = lbl_cy - _LABEL_HEIGHT_PX / 2

        # Extract the edge path waypoints from the pos attribute.
        edge_path: list[list[float]] | None = None
        if "pos" in gv_edge:
            ctrl_pts, arrow_pt = _parse_edge_pos(gv_edge["pos"])
            # Convert all points from Graphviz coordinates to layout px.
            ctrl_px = [list(gv_to_px(p[0], p[1])) for p in ctrl_pts]
            arrow_px = list(gv_to_px(arrow_pt[0], arrow_pt[1])) if arrow_pt else None
            waypoints = _simplify_ortho_path(ctrl_px, arrow_px)
            if len(waypoints) >= 2:
                edge_path = waypoints

        layouted_edges.append(
            input_edge.model_copy(
                update={
                    "is_back_edge": is_back,
                    "label_x": label_x,
                    "label_y": label_y,
                    "edge_path": edge_path,
                }
            )
        )

    seen = {(e.source_id, e.target_id) for e in layouted_edges}
    for edge in graph.edges:
        if (edge.source_id, edge.target_id) not in seen:
            logger.warning(
                "Edge %s->%s not in Graphviz output; appended as-is",
                edge.source_id,
                edge.target_id,
            )
            layouted_edges.append(edge)

    back_count = sum(e.is_back_edge for e in layouted_edges)
    logger.info(
        "Layout (Graphviz dot ortho): %d nodes, %d edges (%d back-edges)",
        len(layouted_nodes),
        len(layouted_edges),
        back_count,
    )
    return LayoutedGraph(nodes=layouted_nodes, edges=layouted_edges)


def _find_input_edge(
    edges: list[DiagramEdge], source_id: str, target_id: str
) -> DiagramEdge | None:
    for e in edges:
        if e.source_id == source_id and e.target_id == target_id:
            return e
    return None
