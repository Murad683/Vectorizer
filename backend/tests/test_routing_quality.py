"""Router clearance / crossing rules and the stress battery (offline).

The battery draws every synthetic topology with Graphviz as a "scan", runs
the production detection + layout on it and checks ``quality_issues``.
"""

import pytest

from app.core.diagram_quality import quality_issues
from app.core.layout_boxes import _CROSS_COST, _Box, _Router
from app.models.diagram import DiagramEdge, LayoutedGraph, LayoutedNode, ShapeType
from scripts.stress_layout import run_one
from tests.test_no_diagonal import ALL_TOPOLOGIES

# Synthetic drawings whose SOURCE already tangles two lines (Graphviz puts an
# arrow in and out of the same side almost on top of each other).
_KNOWN_SOURCE_TANGLES = {"asymmetric_branches", "double_back_edge"}
_BATTERY = [(n, g) for n, g in ALL_TOPOLOGIES if len(g.nodes) >= 2]


@pytest.mark.parametrize("name,graph", _BATTERY, ids=[n for n, _ in _BATTERY])
def test_stress_battery(name: str, graph: object) -> None:
    detected, symbols, exact, issues = run_one(graph)  # type: ignore[arg-type]
    assert exact
    if name != "self_loop":  # Graphviz draws a self-loop as a closed box
        assert detected == symbols
    if name in _KNOWN_SOURCE_TANGLES:
        assert len(issues) <= 1
    else:
        assert issues == []


def test_crossing_on_a_grid_vertex_is_charged() -> None:
    """A path split into two steps exactly at a crossing used to pay for
    neither step; half-open intervals charge it once."""
    boxes = {"a": _Box(0, 0, 50, 30), "b": _Box(200, 200, 50, 30)}
    router = _Router(boxes, [])
    router.used.append((100.0, 100.0, 300.0, 100.0, "other", 0))  # horizontal
    first = router._penalty((150.0, 50.0), (150.0, 100.0), "b")
    second = router._penalty((150.0, 100.0), (150.0, 150.0), "b")
    assert first + second == pytest.approx(_CROSS_COST)


def _node(nid: str, x: float, y: float) -> LayoutedNode:
    return LayoutedNode(
        id=nid, shape_type=ShapeType.PROCESS, text=nid, x=x, y=y, width=100, height=40
    )


def test_quality_flags_a_line_squeezing_past_a_symbol() -> None:
    lg = LayoutedGraph(
        nodes=[_node("a", 0, 0), _node("b", 0, 200), _node("c", 106, 80)],
        edges=[
            DiagramEdge(
                source_id="a",
                target_id="b",
                exact_route=True,
                edge_path=[[100, 20], [103, 20], [103, 220], [100, 220]],
            )
        ],
    )
    assert any("close to node c" in i for i in quality_issues(lg))


def test_quality_flags_parallel_lines_too_close() -> None:
    lg = LayoutedGraph(
        nodes=[_node("a", 0, 0), _node("b", 300, 0), _node("c", 0, 300)],
        edges=[
            DiagramEdge(
                source_id="a",
                target_id="b",
                exact_route=True,
                edge_path=[[50, 40], [50, 100], [350, 100], [350, 40]],
            ),
            DiagramEdge(
                source_id="b",
                target_id="c",
                exact_route=True,
                edge_path=[[300, 20], [200, 20], [200, 103], [60, 103], [60, 300]],
            ),
        ],
    )
    assert any("lines too close" in i for i in quality_issues(lg))
