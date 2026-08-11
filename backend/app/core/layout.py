"""Deterministic, rule-based layout engine for flowchart graphs.

Turns a topological ``DiagramGraph`` into a ``LayoutedGraph`` with
concrete x/y/width/height per node. No AI involved — cheap and
predictable.

Algorithm (tuned for the diagrams in these test materials — a linear
flow with one decision splitting into two branches that may rejoin or
loop back):

1. Start at the node with no incoming edges (fallback: first node) and
   walk the main flow top-to-bottom in a single centre column.
2. At a DECISION node with two outgoing edges, the first edge's target
   starts a left sub-column and the second edge's target a right
   sub-column, both beginning one row below the decision.
3. Each branch walks downward in its own column. Reaching an
   already-placed node ends the branch; if that node lies above the
   current one, the connecting edge is flagged ``is_back_edge`` so
   renderers route it around the columns instead of straight through.
4. Node sizes are fixed per shape type, widened when the text would not
   fit at the standard font size.
"""

import logging

from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    DiagramNode,
    LayoutedGraph,
    LayoutedNode,
    ShapeType,
)

logger = logging.getLogger(__name__)

# Default node sizes in layout px (width, height).
NODE_SIZES: dict[ShapeType, tuple[float, float]] = {
    ShapeType.PROCESS: (160.0, 50.0),
    ShapeType.INPUT_OUTPUT: (160.0, 50.0),
    ShapeType.DECISION: (180.0, 90.0),
    ShapeType.TERMINATOR: (140.0, 50.0),
}

_MARGIN = 40.0
_ROW_GAP = 50.0
_COLUMN_GAP = 60.0
# Rough per-character width at the standard 12 pt font, used to widen
# nodes whose text would otherwise not fit.
_CHAR_WIDTH = 8.0
_TEXT_PADDING = 30.0


def _node_size(node: DiagramNode) -> tuple[float, float]:
    """Return (width, height) for a node, widened for long text."""
    width, height = NODE_SIZES[node.shape_type]
    needed = len(node.text) * _CHAR_WIDTH + _TEXT_PADDING
    return max(width, needed), height


def layout_graph(graph: DiagramGraph) -> LayoutedGraph:
    """Compute geometry for every node of ``graph``.

    Returns a ``LayoutedGraph`` whose edges are copies of the input edges
    with ``is_back_edge`` set where a loop was detected.
    """
    if not graph.nodes:
        return LayoutedGraph(nodes=[], edges=[])

    outgoing: dict[str, list[DiagramEdge]] = {n.id: [] for n in graph.nodes}
    has_incoming: set[str] = set()
    for edge in graph.edges:
        if edge.source_id in outgoing:
            outgoing[edge.source_id].append(edge)
        has_incoming.add(edge.target_id)

    start = next((n for n in graph.nodes if n.id not in has_incoming), graph.nodes[0])

    # column -> horizontal centre offset relative to the main column.
    max_width = max(_node_size(n)[0] for n in graph.nodes)
    column_x = {
        "main": 0.0,
        "left": -(max_width + _COLUMN_GAP),
        "right": max_width + _COLUMN_GAP,
    }

    placed: dict[str, tuple[str, int]] = {}  # id -> (column, row)

    def place_run(node_id: str, column: str, row: int) -> None:
        """Walk a chain of nodes downward in one column until it ends."""
        current: str | None = node_id
        current_row = row
        while current is not None and current not in placed:
            placed[current] = (column, current_row)
            edges = outgoing[current]
            if len(edges) >= 2:
                # A decision (or any fork): branch targets go side by side.
                branch_row = current_row + 1
                place_run(edges[0].target_id, "left", branch_row)
                place_run(edges[1].target_id, "right", branch_row)
                for extra in edges[2:]:  # defensive: >2-way forks
                    place_run(extra.target_id, "right", branch_row)
                return
            current = edges[0].target_id if edges else None
            current_row += 1

    place_run(start.id, "main", 0)
    # Any nodes unreachable from the start (disconnected fragments) are
    # appended below the layout so nothing is silently dropped.
    next_row = max((row for _, row in placed.values()), default=-1) + 1
    for node in graph.nodes:
        if node.id not in placed:
            logger.warning("Node %s unreachable from start; appended below", node.id)
            place_run(node.id, "main", next_row)
            next_row = max(row for _, row in placed.values()) + 1

    row_height = max(h for _, h in NODE_SIZES.values())  # uniform rows
    layouted_nodes: list[LayoutedNode] = []
    min_centre = min(column_x[col] for col, _ in placed.values())
    for node in graph.nodes:
        column, row = placed[node.id]
        width, height = _node_size(node)
        centre_x = column_x[column] - min_centre + max_width / 2 + _MARGIN
        top_y = _MARGIN + row * (row_height + _ROW_GAP) + (row_height - height) / 2
        layouted_nodes.append(
            LayoutedNode(
                **node.model_dump(),
                x=centre_x - width / 2,
                y=top_y,
                width=width,
                height=height,
            )
        )

    layouted_edges: list[DiagramEdge] = []
    for edge in graph.edges:
        is_back = (
            edge.source_id in placed
            and edge.target_id in placed
            and placed[edge.target_id][1] <= placed[edge.source_id][1]
            and edge.source_id != edge.target_id
        )
        layouted_edges.append(edge.model_copy(update={"is_back_edge": is_back}))

    back_count = sum(e.is_back_edge for e in layouted_edges)
    logger.info(
        "Layout: %d nodes, %d edges (%d back-edges)",
        len(layouted_nodes),
        len(layouted_edges),
        back_count,
    )
    return LayoutedGraph(nodes=layouted_nodes, edges=layouted_edges)
