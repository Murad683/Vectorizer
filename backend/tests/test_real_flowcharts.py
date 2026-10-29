"""End-to-end offline checks on the real textbook flowcharts.

The model's answer is the hand-built ideal from ``diagram_fixtures``; its
set-of-mark answer is simulated against the CURRENT detector. Everything
after the API call (detection, layout, quality) is the production code.
"""

import pytest

from app.core.diagram_detect import detect
from app.core.diagram_quality import quality_issues
from app.core.layout import layout_graph
from app.core.vision.postprocess import attach_geometry
from app.models.diagram import DiagramGraph, LayoutedGraph
from tests.diagram_fixtures import FIXTURES, SAMPLES, simulate_marks


def _run(name: str) -> tuple[DiagramGraph, LayoutedGraph]:
    file, make = FIXTURES[name]
    image = (SAMPLES / file).read_bytes()
    detection = detect(image)
    assert detection is not None
    graph = make()
    simulate_marks(graph, detection)
    attach_geometry(graph, image, detection, marked=True)
    return graph, layout_graph(graph.model_copy(deep=True))


@pytest.mark.parametrize("name", list(FIXTURES))
def test_every_edge_is_routed(name: str) -> None:
    graph, lg = _run(name)
    assert len(lg.edges) == len(graph.edges)
    assert all(e.edge_path and len(e.edge_path) >= 2 for e in lg.edges)


@pytest.mark.parametrize("name", ["fahrenheit_celsius", "sum_loop"])
def test_framed_only_diagrams_use_exact_geometry(name: str) -> None:
    graph, lg = _run(name)
    assert graph.boxes_detected
    assert quality_issues(lg) == []
