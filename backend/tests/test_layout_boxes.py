"""Tests for the source-position layout (``app/core/layout_boxes.py``).

All offline: node boxes are hand-measured from the sample flowcharts, so
no vision API call is involved.
"""

import time

import pytest

from app.core.layout import layout_graph
from app.core.layout_boxes import layout_from_boxes
from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    DiagramNode,
    LayoutedGraph,
    PortSide as P,
    ShapeType as T,
)


def _node(nid: str, shape: T, text: str, box: tuple[int, int, int, int],
          size: tuple[int, int]) -> DiagramNode:
    """``box`` is pixel (x0, y0, x1, y1) in an image of ``size`` (w, h)."""
    w, h = size
    x0, y0, x1, y1 = box
    return DiagramNode(
        id=nid, shape_type=shape, text=text,
        box_2d=[round(y0 / h * 1000), round(x0 / w * 1000),
                round(y1 / h * 1000), round(x1 / w * 1000)],
    )


def _edge(s: str, t: str, label: str | None = None,
          port: P | None = None) -> DiagramEdge:
    return DiagramEdge(source_id=s, target_id=t, label=label, source_port=port)


@pytest.fixture
def euclid() -> DiagramGraph:
    sz = (399, 500)
    return DiagramGraph(
        image_width=sz[0], image_height=sz[1],
        nodes=[
            _node("n1", T.TERMINATOR, "Başlanğıc", (128, 82, 190, 107), sz),
            _node("n2", T.INPUT_OUTPUT, "a,b", (127, 123, 185, 155), sz),
            _node("n3", T.PROCESS, "c=a*b", (125, 172, 182, 197), sz),
            _node("n4", T.DECISION, "a==b", (108, 218, 200, 262), sz),
            _node("n5", T.DECISION, "a>b", (108, 285, 190, 332), sz),
            _node("n6", T.PROCESS, "a=a-b", (52, 338, 105, 362), sz),
            _node("n7", T.PROCESS, "b=b-a", (192, 338, 245, 362), sz),
            _node("n8", T.PROCESS, "d=c/a", (258, 262, 318, 288), sz),
            _node("n9", T.INPUT_OUTPUT, "d", (275, 310, 312, 345), sz),
            _node("n10", T.TERMINATOR, "Son", (262, 365, 322, 390), sz),
        ],
        edges=[
            _edge("n1", "n2"), _edge("n2", "n3"), _edge("n3", "n4"),
            _edge("n4", "n8", "Hə", P.RIGHT), _edge("n4", "n5", "Yox", P.BOTTOM),
            _edge("n5", "n6", "Hə", P.LEFT), _edge("n5", "n7", "Yox", P.RIGHT),
            _edge("n6", "n4"), _edge("n7", "n4"),
            _edge("n8", "n9"), _edge("n9", "n10"),
        ],
    )


@pytest.fixture
def loop_chart() -> DiagramGraph:
    sz = (1944, 1904)
    return DiagramGraph(
        image_width=sz[0], image_height=sz[1],
        nodes=[
            _node("n1", T.TERMINATOR, "Başlanğıc", (675, 105, 1205, 270), sz),
            _node("n2", T.INPUT_OUTPUT, "n", (695, 395, 1185, 530), sz),
            _node("n3", T.PROCESS, "a = 3", (732, 648, 1150, 830), sz),
            _node("n4", T.DECISION, "a < 164", (640, 955, 1245, 1190), sz),
            _node("n5", T.INPUT_OUTPUT, "n", (40, 1295, 527, 1430), sz),
            _node("n6", T.TERMINATOR, "Son", (105, 1560, 460, 1725), sz),
            _node("n7", T.PROCESS, "n = n - 8", (1393, 1295, 1812, 1475), sz),
            _node("n8", T.PROCESS, "a = a + 16", (1393, 1598, 1812, 1780), sz),
        ],
        edges=[
            _edge("n1", "n2"), _edge("n2", "n3"), _edge("n3", "n4"),
            _edge("n4", "n5", "Yox", P.LEFT), _edge("n5", "n6"),
            _edge("n4", "n7", "Hə", P.RIGHT), _edge("n7", "n8"),
            _edge("n8", "n4"),
        ],
    )


def _assert_valid(lg: LayoutedGraph) -> None:
    nodes = {n.id: n for n in lg.nodes}
    for e in lg.edges:
        assert e.exact_route and e.edge_path and len(e.edge_path) >= 2
        path = e.edge_path
        for p, q in zip(path, path[1:]):
            # Only axis-aligned segments, never diagonal.
            assert abs(p[0] - q[0]) < 0.01 or abs(p[1] - q[1]) < 0.01
            lo_x, hi_x = min(p[0], q[0]), max(p[0], q[0])
            lo_y, hi_y = min(p[1], q[1]), max(p[1], q[1])
            for n in lg.nodes:
                inside = (
                    lo_x < n.x + n.width and hi_x > n.x
                    and lo_y < n.y + n.height and hi_y > n.y
                )
                if n.id in (e.source_id, e.target_id):
                    continue  # own endpoints touch the border
                assert not inside, f"{e.source_id}->{e.target_id} crosses {n.id}"
        # Endpoints sit on the node borders.
        for pt, node in ((path[0], nodes[e.source_id]), (path[-1], nodes[e.target_id])):
            on_x = node.x - 0.5 <= pt[0] <= node.x + node.width + 0.5
            on_y = node.y - 0.5 <= pt[1] <= node.y + node.height + 0.5
            assert on_x and on_y


def test_euclid_routes_are_orthogonal_and_clear(euclid: DiagramGraph) -> None:
    lg = layout_from_boxes(euclid)
    assert lg is not None
    assert len(lg.nodes) == 10 and len(lg.edges) == 11
    _assert_valid(lg)


def test_loop_keeps_original_arrangement(loop_chart: DiagramGraph) -> None:
    lg = layout_from_boxes(loop_chart)
    assert lg is not None
    _assert_valid(lg)
    by_id = {n.id: n for n in lg.nodes}
    # Same left/right arrangement as the source image.
    assert by_id["n5"].x < by_id["n4"].x < by_id["n7"].x
    # The loop-back edge is flagged and reaches the decision.
    back = [e for e in lg.edges if e.is_back_edge]
    assert [(e.source_id, e.target_id) for e in back] == [("n8", "n4")]


def test_straight_flow_shares_one_column(loop_chart: DiagramGraph) -> None:
    lg = layout_from_boxes(loop_chart)
    assert lg is not None
    centres = {n.id: n.x + n.width / 2 for n in lg.nodes}
    assert centres["n1"] == pytest.approx(centres["n2"], abs=0.5)
    assert centres["n2"] == pytest.approx(centres["n3"], abs=0.5)


def test_branch_labels_are_placed(euclid: DiagramGraph) -> None:
    lg = layout_from_boxes(euclid)
    assert lg is not None
    labelled = [e for e in lg.edges if e.label]
    assert len(labelled) == 4
    assert all(e.label_x is not None and e.label_y is not None for e in labelled)


def test_missing_boxes_returns_none_and_graphviz_is_used(
    euclid: DiagramGraph,
) -> None:
    euclid.nodes[0].box_2d = None
    assert layout_from_boxes(euclid) is None
    lg = layout_graph(euclid)  # Graphviz fallback still works
    assert len(lg.nodes) == 10
    assert not any(e.exact_route for e in lg.edges)


def test_degenerate_box_returns_none(euclid: DiagramGraph) -> None:
    euclid.nodes[1].box_2d = [500, 500, 500, 500]
    assert layout_from_boxes(euclid) is None


def test_self_loop_does_not_crash() -> None:
    sz = (400, 400)
    g = DiagramGraph(
        image_width=400, image_height=400,
        nodes=[_node("n1", T.PROCESS, "x = x + 1", (100, 100, 220, 150), sz)],
        edges=[_edge("n1", "n1")],
    )
    lg = layout_from_boxes(g)
    assert lg is not None and lg.edges[0].exact_route


def test_routing_is_fast(euclid: DiagramGraph) -> None:
    start = time.perf_counter()
    layout_from_boxes(euclid)
    assert time.perf_counter() - start < 3.0
