"""Layout engine tests — Graphviz dot backend (Phase 21)."""

from app.core.layout import layout_graph
from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    DiagramNode,
    LayoutedNode,
    PortSide,
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
        assert back[0].source_id == "n8"
        assert back[0].target_id == "n4"

    def test_all_nodes_placed_with_positive_geometry(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        assert len(layouted.nodes) == len(flowchart_graph.nodes)
        for node in layouted.nodes:
            assert node.x >= 0 and node.y >= 0
            assert node.width > 0 and node.height > 0

    def test_main_flow_is_top_to_bottom(self, flowchart_graph: DiagramGraph) -> None:
        layouted = layout_graph(flowchart_graph)
        ys = [layouted.node_by_id(i).y for i in ("n1", "n2", "n3", "n4")]
        assert ys == sorted(ys)

    def test_layout_is_deterministic(self, flowchart_graph: DiagramGraph) -> None:
        first = layout_graph(flowchart_graph)
        second = layout_graph(flowchart_graph)
        assert first.model_dump() == second.model_dump()

    def test_decision_branches_below_decision(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        decision = layouted.node_by_id("n4")
        n5 = layouted.node_by_id("n5")
        n7 = layouted.node_by_id("n7")
        assert n5.y > decision.y
        assert n7.y > decision.y


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


def _two_decision_graph() -> DiagramGraph:
    return DiagramGraph(
        nodes=[
            DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Başlanğıc"),
            DiagramNode(id="n2", shape_type=ShapeType.DECISION, text="a < 10"),
            DiagramNode(id="n3", shape_type=ShapeType.PROCESS, text="a = a + 1"),
            DiagramNode(id="n4", shape_type=ShapeType.DECISION, text="b < 5"),
            DiagramNode(id="n5", shape_type=ShapeType.PROCESS, text="b = b + 1"),
            DiagramNode(id="n6", shape_type=ShapeType.INPUT_OUTPUT, text="çap et"),
            DiagramNode(id="n7", shape_type=ShapeType.PROCESS, text="c = a + b"),
            DiagramNode(id="n8", shape_type=ShapeType.TERMINATOR, text="Son"),
        ],
        edges=[
            DiagramEdge(source_id="n1", target_id="n2"),
            DiagramEdge(source_id="n2", target_id="n3", label="Hə"),
            DiagramEdge(source_id="n2", target_id="n4", label="Yox"),
            DiagramEdge(source_id="n4", target_id="n5", label="Hə"),
            DiagramEdge(source_id="n4", target_id="n6", label="Yox"),
            DiagramEdge(source_id="n3", target_id="n7"),
            DiagramEdge(source_id="n5", target_id="n7"),
            DiagramEdge(source_id="n6", target_id="n7"),
            DiagramEdge(source_id="n7", target_id="n8"),
        ],
    )


class TestMultiForkCollision:
    def test_no_two_nodes_overlap(self) -> None:
        layouted = layout_graph(_two_decision_graph())
        nodes = layouted.nodes
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                assert not _overlaps(a, b), f"{a.id} overlaps {b.id}"

    def test_all_nodes_get_distinct_positions(self) -> None:
        layouted = layout_graph(_two_decision_graph())
        positions = [(n.x, n.y) for n in layouted.nodes]
        assert len(positions) == len(set(positions))

    def test_terminal_node_incoming_edge_from_correct_source(self) -> None:
        layouted = layout_graph(_two_decision_graph())
        incoming = [e for e in layouted.edges if e.target_id == "n8"]
        assert len(incoming) == 1
        assert incoming[0].source_id == "n7"


class TestBackEdgeLayout:
    """Back-edges must be flagged and nodes must not overlap."""

    def test_back_edges_flagged(self, collision_graph: DiagramGraph) -> None:
        layouted = layout_graph(collision_graph)
        backs = [e for e in layouted.edges if e.is_back_edge]
        assert len(backs) == 2
        sources = {e.source_id for e in backs}
        assert sources == {"n5", "n6"}

    def test_no_two_nodes_overlap(self, collision_graph: DiagramGraph) -> None:
        layouted = layout_graph(collision_graph)
        nodes = layouted.nodes
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                assert not _overlaps(a, b), f"{a.id} overlaps {b.id}"

    def test_all_nodes_positive_geometry(
        self, collision_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(collision_graph)
        for node in layouted.nodes:
            assert node.x >= 0 and node.y >= 0
            assert node.width > 0 and node.height > 0


class TestEdgeLabelPositions:
    """Graphviz must provide non-overlapping label positions."""

    def test_labeled_edges_have_label_coordinates(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        for edge in layouted.edges:
            if edge.label:
                assert edge.label_x is not None, (
                    f"Edge {edge.source_id}->{edge.target_id} missing label_x"
                )
                assert edge.label_y is not None, (
                    f"Edge {edge.source_id}->{edge.target_id} missing label_y"
                )

    def test_sibling_labels_distinct_positions(
        self, collision_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(collision_graph)
        n4_labels = [
            (e.label_x, e.label_y)
            for e in layouted.edges
            if e.source_id == "n4" and e.label
        ]
        assert len(n4_labels) == 2
        assert n4_labels[0] != n4_labels[1]

    def test_y_axis_inversion_correct(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        start = layouted.node_by_id("n1")
        for node in layouted.nodes:
            if node.id != "n1":
                assert node.y >= start.y, (
                    f"Node {node.id} has y={node.y} above start y={start.y}"
                )

    def test_three_way_fork_labels_no_overlap(self) -> None:
        graph = DiagramGraph(
            nodes=[
                DiagramNode(id="s", shape_type=ShapeType.TERMINATOR, text="Start"),
                DiagramNode(id="d", shape_type=ShapeType.DECISION, text="x?"),
                DiagramNode(id="a", shape_type=ShapeType.PROCESS, text="A"),
                DiagramNode(id="b", shape_type=ShapeType.PROCESS, text="B"),
                DiagramNode(id="c", shape_type=ShapeType.PROCESS, text="C"),
            ],
            edges=[
                DiagramEdge(source_id="s", target_id="d"),
                DiagramEdge(source_id="d", target_id="a", label="L1"),
                DiagramEdge(source_id="d", target_id="b", label="L2"),
                DiagramEdge(source_id="d", target_id="c", label="L3"),
            ],
        )
        layouted = layout_graph(graph)
        label_positions = [
            (e.label_x, e.label_y)
            for e in layouted.edges
            if e.label
        ]
        assert len(label_positions) == 3
        assert len(set(label_positions)) == 3


class TestOrthoEdgePaths:
    """Phase 22: verify Graphviz ortho edge paths are extracted and
    that no connector segment crosses any node's bounding box."""

    def test_all_edges_have_paths(self, flowchart_graph: DiagramGraph) -> None:
        layouted = layout_graph(flowchart_graph)
        for edge in layouted.edges:
            assert edge.edge_path is not None, (
                f"Edge {edge.source_id}->{edge.target_id} missing edge_path"
            )
            assert len(edge.edge_path) >= 2, (
                f"Edge {edge.source_id}->{edge.target_id} path too short"
            )

    def test_edge_paths_are_axis_aligned(self, flowchart_graph: DiagramGraph) -> None:
        layouted = layout_graph(flowchart_graph)
        for edge in layouted.edges:
            if not edge.edge_path or len(edge.edge_path) < 2:
                continue
            for i in range(1, len(edge.edge_path)):
                prev = edge.edge_path[i - 1]
                cur = edge.edge_path[i]
                same_x = abs(prev[0] - cur[0]) < 1.0
                same_y = abs(prev[1] - cur[1]) < 1.0
                assert same_x or same_y, (
                    f"Edge {edge.source_id}->{edge.target_id} segment {i} "
                    f"not axis-aligned: {prev} -> {cur}"
                )

    def test_no_connector_crosses_any_node(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(flowchart_graph)
        for edge in layouted.edges:
            if not edge.edge_path or len(edge.edge_path) < 2:
                continue
            for i in range(1, len(edge.edge_path)):
                seg_start = edge.edge_path[i - 1]
                seg_end = edge.edge_path[i]
                for node in layouted.nodes:
                    if node.id in (edge.source_id, edge.target_id):
                        continue
                    assert not _segment_crosses_node(
                        seg_start, seg_end, node
                    ), (
                        f"Edge {edge.source_id}->{edge.target_id} segment "
                        f"({seg_start}->{seg_end}) crosses node {node.id} "
                        f"at ({node.x},{node.y},{node.width},{node.height})"
                    )

    def test_no_connector_crosses_any_label(
        self, flowchart_graph: DiagramGraph
    ) -> None:
        from app.core.layout import _LABEL_HEIGHT_PX, _LABEL_WIDTH_PX

        layouted = layout_graph(flowchart_graph)
        for edge in layouted.edges:
            if not edge.edge_path or len(edge.edge_path) < 2:
                continue
            for other in layouted.edges:
                if other.label_x is None or other.label_y is None:
                    continue
                if other is edge:
                    continue
                label_node = LayoutedNode(
                    id="__label__",
                    shape_type=ShapeType.PROCESS,
                    text="",
                    x=other.label_x,
                    y=other.label_y,
                    width=_LABEL_WIDTH_PX,
                    height=_LABEL_HEIGHT_PX,
                )
                for i in range(1, len(edge.edge_path)):
                    seg_start = edge.edge_path[i - 1]
                    seg_end = edge.edge_path[i]
                    assert not _segment_crosses_node(
                        seg_start, seg_end, label_node
                    ), (
                        f"Edge {edge.source_id}->{edge.target_id} segment "
                        f"crosses label '{other.label}' at "
                        f"({other.label_x},{other.label_y})"
                    )

    def test_complex_topology_no_crossing(self) -> None:
        """3+ forks with nested loop-backs — no segment crosses any
        unrelated node."""
        graph = DiagramGraph(
            nodes=[
                DiagramNode(id="s", shape_type=ShapeType.TERMINATOR, text="Start"),
                DiagramNode(id="d1", shape_type=ShapeType.DECISION, text="d1?"),
                DiagramNode(id="a", shape_type=ShapeType.PROCESS, text="A"),
                DiagramNode(id="d2", shape_type=ShapeType.DECISION, text="d2?"),
                DiagramNode(id="b", shape_type=ShapeType.PROCESS, text="B"),
                DiagramNode(id="c", shape_type=ShapeType.PROCESS, text="C"),
                DiagramNode(id="d3", shape_type=ShapeType.DECISION, text="d3?"),
                DiagramNode(id="x", shape_type=ShapeType.PROCESS, text="X"),
                DiagramNode(id="y", shape_type=ShapeType.PROCESS, text="Y"),
                DiagramNode(id="e", shape_type=ShapeType.TERMINATOR, text="End"),
            ],
            edges=[
                DiagramEdge(source_id="s", target_id="d1"),
                DiagramEdge(source_id="d1", target_id="a", label="L"),
                DiagramEdge(source_id="d1", target_id="d2", label="R"),
                DiagramEdge(source_id="a", target_id="e"),
                DiagramEdge(source_id="d2", target_id="b", label="L"),
                DiagramEdge(source_id="d2", target_id="d3", label="R"),
                DiagramEdge(source_id="b", target_id="d1"),
                DiagramEdge(source_id="d3", target_id="x", label="L"),
                DiagramEdge(source_id="d3", target_id="y", label="R"),
                DiagramEdge(source_id="x", target_id="d2"),
                DiagramEdge(source_id="y", target_id="e"),
            ],
        )
        layouted = layout_graph(graph)
        # All edges must have paths.
        for edge in layouted.edges:
            assert edge.edge_path is not None
        # No segment crosses any unrelated node.
        for edge in layouted.edges:
            for i in range(1, len(edge.edge_path)):
                seg_start = edge.edge_path[i - 1]
                seg_end = edge.edge_path[i]
                for node in layouted.nodes:
                    if node.id in (edge.source_id, edge.target_id):
                        continue
                    assert not _segment_crosses_node(
                        seg_start, seg_end, node
                    ), (
                        f"Edge {edge.source_id}->{edge.target_id} "
                        f"crosses node {node.id}"
                    )

    def test_collision_graph_no_crossing(
        self, collision_graph: DiagramGraph
    ) -> None:
        layouted = layout_graph(collision_graph)
        for edge in layouted.edges:
            assert edge.edge_path is not None
            for i in range(1, len(edge.edge_path)):
                seg_start = edge.edge_path[i - 1]
                seg_end = edge.edge_path[i]
                for node in layouted.nodes:
                    if node.id in (edge.source_id, edge.target_id):
                        continue
                    assert not _segment_crosses_node(
                        seg_start, seg_end, node
                    ), (
                        f"Edge {edge.source_id}->{edge.target_id} "
                        f"crosses node {node.id}"
                    )


class TestSafetyNet:
    """Phase 22: detect and flag edges requiring more bends than
    available OOXML presets support."""

    def test_many_bends_logged_and_capped(self) -> None:
        """Build a wide graph where Graphviz may generate paths with
        many bends. Verify choose_connector_from_path caps at 4."""
        from app.core.render.pptx_shapes import choose_connector_from_path

        # Synthetic path with 6 bends (7 waypoints).
        path: list[list[float]] = [
            [0.0, 0.0],
            [100.0, 0.0],
            [100.0, 50.0],
            [200.0, 50.0],
            [200.0, 100.0],
            [300.0, 100.0],
            [300.0, 150.0],
            [400.0, 150.0],
        ]
        dummy_src = LayoutedNode(
            id="s", shape_type=ShapeType.PROCESS, text="S",
            x=0.0, y=0.0, width=20.0, height=20.0,
        )
        dummy_tgt = LayoutedNode(
            id="t", shape_type=ShapeType.PROCESS, text="T",
            x=380.0, y=130.0, width=20.0, height=20.0,
        )
        preset, _, _, _, bend_count = choose_connector_from_path(
            path, dummy_src, dummy_tgt
        )
        assert preset == "bentConnector5"
        assert bend_count == 4

    def test_straight_path(self) -> None:
        from app.core.render.pptx_shapes import choose_connector_from_path

        path = [[100.0, 0.0], [100.0, 200.0]]
        src = LayoutedNode(
            id="s", shape_type=ShapeType.PROCESS, text="S",
            x=80.0, y=0.0, width=40.0, height=20.0,
        )
        tgt = LayoutedNode(
            id="t", shape_type=ShapeType.PROCESS, text="T",
            x=80.0, y=180.0, width=40.0, height=20.0,
        )
        preset, start_idx, end_idx, adjs, bends = choose_connector_from_path(
            path, src, tgt
        )
        assert preset == "straightConnector1"
        assert bends == 0
        assert adjs == {}

    def test_one_bend_path(self) -> None:
        """A single-bend elbow needs PERPENDICULAR connection sites (one
        top/bottom, the other left/right) — that's the only pairing a
        2-segment L can satisfy. tgt is positioned so the path's end
        point lands on its LEFT site (perpendicular to src's TOP exit),
        not its TOP/BOTTOM (which _enforce_min_bends_for_axis correctly
        promotes to a 2-bend Z-shape, since two same-axis sites can't be
        joined by a single valid bend unless already aligned)."""
        from app.core.render.pptx_shapes import choose_connector_from_path

        path = [[100.0, 0.0], [100.0, 100.0], [200.0, 100.0]]
        src = LayoutedNode(
            id="s", shape_type=ShapeType.PROCESS, text="S",
            x=80.0, y=0.0, width=40.0, height=20.0,
        )
        tgt = LayoutedNode(
            id="t", shape_type=ShapeType.PROCESS, text="T",
            x=200.0, y=90.0, width=40.0, height=20.0,
        )
        preset, _, _, _, bends = choose_connector_from_path(path, src, tgt)
        assert preset == "bentConnector2"
        assert bends == 1


def _segment_crosses_node(
    seg_start: list[float], seg_end: list[float], node: LayoutedNode
) -> bool:
    """Check if an axis-aligned segment crosses a node's bounding box
    (excluding the node's own edges — the segment must pass THROUGH
    the interior)."""
    sx, sy = seg_start
    ex, ey = seg_end

    n_left = node.x
    n_right = node.x + node.width
    n_top = node.y
    n_bottom = node.y + node.height

    margin = 2.0

    if abs(sy - ey) < 1.0:
        # Horizontal segment.
        y = sy
        if y <= n_top + margin or y >= n_bottom - margin:
            return False
        seg_left = min(sx, ex)
        seg_right = max(sx, ex)
        return seg_left < n_right - margin and seg_right > n_left + margin
    elif abs(sx - ex) < 1.0:
        # Vertical segment.
        x = sx
        if x <= n_left + margin or x >= n_right - margin:
            return False
        seg_top = min(sy, ey)
        seg_bottom = max(sy, ey)
        return seg_top < n_bottom - margin and seg_bottom > n_top + margin
    return False


class TestSourcePortLayout:
    """Phase 25: source_port hints from vision guide Graphviz layout."""

    @staticmethod
    def _decision_with_right_branch() -> DiagramGraph:
        return DiagramGraph(
            nodes=[
                DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Start"),
                DiagramNode(id="n2", shape_type=ShapeType.DECISION, text="x > 0"),
                DiagramNode(id="n3", shape_type=ShapeType.PROCESS, text="A"),
                DiagramNode(id="n4", shape_type=ShapeType.PROCESS, text="B"),
                DiagramNode(id="n5", shape_type=ShapeType.TERMINATOR, text="End"),
            ],
            edges=[
                DiagramEdge(source_id="n1", target_id="n2"),
                DiagramEdge(
                    source_id="n2", target_id="n3", label="Hə",
                    source_port=PortSide.RIGHT,
                ),
                DiagramEdge(
                    source_id="n2", target_id="n4", label="Yox",
                    source_port=PortSide.BOTTOM,
                ),
                DiagramEdge(source_id="n3", target_id="n5"),
                DiagramEdge(source_id="n4", target_id="n5"),
            ],
        )

    def test_right_branch_same_y_level(self) -> None:
        """A RIGHT-ported branch should place its target at roughly
        the same Y level as the decision (rank=same)."""
        layouted = layout_graph(self._decision_with_right_branch())
        decision = layouted.node_by_id("n2")
        right_target = layouted.node_by_id("n3")
        decision_cy = decision.y + decision.height / 2
        target_cy = right_target.y + right_target.height / 2
        assert abs(decision_cy - target_cy) < 5.0, (
            f"RIGHT branch target n3 not at same Y: "
            f"decision cy={decision_cy:.1f}, target cy={target_cy:.1f}"
        )

    def test_right_branch_is_to_the_right(self) -> None:
        """A RIGHT-ported branch target should be to the right of the decision."""
        layouted = layout_graph(self._decision_with_right_branch())
        decision = layouted.node_by_id("n2")
        right_target = layouted.node_by_id("n3")
        assert right_target.x > decision.x + decision.width / 2

    def test_bottom_branch_is_below(self) -> None:
        """A BOTTOM-ported branch target should be below the decision."""
        layouted = layout_graph(self._decision_with_right_branch())
        decision = layouted.node_by_id("n2")
        bottom_target = layouted.node_by_id("n4")
        assert bottom_target.y > decision.y + decision.height

    def test_no_port_hints_still_works(self) -> None:
        """Edges without source_port should layout without errors."""
        graph = DiagramGraph(
            nodes=[
                DiagramNode(id="a", shape_type=ShapeType.PROCESS, text="A"),
                DiagramNode(id="b", shape_type=ShapeType.PROCESS, text="B"),
            ],
            edges=[DiagramEdge(source_id="a", target_id="b")],
        )
        layouted = layout_graph(graph)
        assert len(layouted.nodes) == 2
        assert layouted.node_by_id("a").y < layouted.node_by_id("b").y

    def test_conflicting_ports_gracefully_ignored(self) -> None:
        """Two edges from the same node claiming the same port should
        be silently ignored (layout still succeeds)."""
        graph = DiagramGraph(
            nodes=[
                DiagramNode(id="d", shape_type=ShapeType.DECISION, text="x?"),
                DiagramNode(id="a", shape_type=ShapeType.PROCESS, text="A"),
                DiagramNode(id="b", shape_type=ShapeType.PROCESS, text="B"),
            ],
            edges=[
                DiagramEdge(
                    source_id="d", target_id="a", label="L",
                    source_port=PortSide.RIGHT,
                ),
                DiagramEdge(
                    source_id="d", target_id="b", label="R",
                    source_port=PortSide.RIGHT,
                ),
            ],
        )
        layouted = layout_graph(graph)
        assert len(layouted.nodes) == 3

    def test_left_branch_same_y_level(self) -> None:
        """A LEFT-ported branch should also use rank=same."""
        graph = DiagramGraph(
            nodes=[
                DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Start"),
                DiagramNode(id="n2", shape_type=ShapeType.DECISION, text="x?"),
                DiagramNode(id="n3", shape_type=ShapeType.PROCESS, text="Left"),
                DiagramNode(id="n4", shape_type=ShapeType.PROCESS, text="Down"),
            ],
            edges=[
                DiagramEdge(source_id="n1", target_id="n2"),
                DiagramEdge(
                    source_id="n2", target_id="n3", label="Hə",
                    source_port=PortSide.LEFT,
                ),
                DiagramEdge(
                    source_id="n2", target_id="n4", label="Yox",
                    source_port=PortSide.BOTTOM,
                ),
            ],
        )
        layouted = layout_graph(graph)
        decision = layouted.node_by_id("n2")
        left_target = layouted.node_by_id("n3")
        decision_cy = decision.y + decision.height / 2
        target_cy = left_target.y + left_target.height / 2
        assert abs(decision_cy - target_cy) < 5.0
