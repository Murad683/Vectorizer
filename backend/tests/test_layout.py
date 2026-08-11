"""Layout engine tests using the real flowchart sample's known structure."""

from app.core.layout import layout_graph
from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    DiagramNode,
    LayoutedNode,
    ShapeType,
)


def _overlaps(a: LayoutedNode, b: LayoutedNode) -> bool:
    return not (
        a.x + a.width <= b.x
        or b.x + b.width <= a.x
        or a.y + a.height <= b.y
        or b.y + b.height <= a.y
    )


class TestFlowchartLayout:
    def test_no_two_nodes_overlap(self, flowchart_graph: DiagramGraph) -> None:
        layouted = layout_graph(flowchart_graph)
        nodes = layouted.nodes
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                assert not _overlaps(a, b), f"{a.id} overlaps {b.id}"

    def test_back_edge_is_flagged(self, flowchart_graph: DiagramGraph) -> None:
        layouted = layout_graph(flowchart_graph)
        back = [e for e in layouted.edges if e.is_back_edge]
        assert len(back) == 1
        assert back[0].source_id == "n8"  # a = a + 16
        assert back[0].target_id == "n4"  # the decision

    def test_all_nodes_placed_with_positive_geometry(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        assert len(layouted.nodes) == len(flowchart_graph.nodes)
        for node in layouted.nodes:
            assert node.x >= 0 and node.y >= 0
            assert node.width > 0 and node.height > 0

    def test_decision_branches_are_side_by_side(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        decision = layouted.node_by_id("n4")
        yox_target = layouted.node_by_id("n5")  # first branch → left
        he_target = layouted.node_by_id("n7")  # second branch → right
        assert yox_target.x + yox_target.width <= decision.x + decision.width
        assert he_target.x >= decision.x
        # Both start below the decision, on the same row.
        assert yox_target.y > decision.y
        assert abs(yox_target.y - he_target.y) < 60

    def test_main_flow_is_top_to_bottom(self, flowchart_graph: DiagramGraph) -> None:
        layouted = layout_graph(flowchart_graph)
        ys = [layouted.node_by_id(i).y for i in ("n1", "n2", "n3", "n4")]
        assert ys == sorted(ys)

    def test_layout_is_deterministic(self, flowchart_graph: DiagramGraph) -> None:
        first = layout_graph(flowchart_graph)
        second = layout_graph(flowchart_graph)
        assert first.model_dump() == second.model_dump()


def test_long_text_widens_node() -> None:
    long_text = "çox uzun bir mətn ilə doldurulmuş proses bloku nümunəsi"
    graph = DiagramGraph(
        nodes=[
            DiagramNode(id="a", shape_type=ShapeType.PROCESS, text="qısa"),
            DiagramNode(id="b", shape_type=ShapeType.PROCESS, text=long_text),
        ],
        edges=[DiagramEdge(source_id="a", target_id="b")],
    )
    layouted = layout_graph(graph)
    assert layouted.node_by_id("b").width > layouted.node_by_id("a").width


def test_empty_graph_returns_empty_layout() -> None:
    layouted = layout_graph(DiagramGraph(nodes=[], edges=[]))
    assert layouted.nodes == []
    assert layouted.edges == []
