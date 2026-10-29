"""End-to-end offline checks on the real textbook flowcharts.

The model's answer is the hand-built ideal from ``diagram_fixtures``; its
set-of-mark answer is simulated against the CURRENT detector. Everything
after the API call (detection, layout, quality) is the production code.
"""

import pytest

from app.core.diagram_detect import detect
from app.core.diagram_quality import quality_issues
from tests.diagram_fixtures import FIXTURES, SAMPLES, run_fixture


@pytest.mark.parametrize("name", list(FIXTURES))
def test_every_edge_is_routed(name: str) -> None:
    graph, lg = run_fixture(name)
    assert len(lg.edges) == len(graph.edges)
    assert all(e.edge_path and len(e.edge_path) >= 2 for e in lg.edges)


@pytest.mark.parametrize(
    "file,count",
    [
        ("quadratic_equation.png", 9),  # 2 frameless formulas are not symbols
        ("fahrenheit_celsius.png", 5),
        ("sp_open_exit.png", 6),
        ("sum_loop.jpg", 9),
    ],
)
def test_detects_every_framed_symbol(file: str, count: int) -> None:
    det = detect((SAMPLES / file).read_bytes())
    assert det is not None
    assert len(det.shapes) == count


def test_broken_diamond_outline_is_recovered_at_full_size() -> None:
    """``D = 0`` and ``P < 10`` have a break at a tip; the recovered box
    must span the whole diamond, like its intact twin above it."""
    for file, intact, broken in (
        ("quadratic_equation.png", 4, 5),
        ("sp_open_exit.png", 2, 5),
    ):
        det = detect((SAMPLES / file).read_bytes())
        assert det is not None
        a, b = det.shapes[intact - 1], det.shapes[broken - 1]
        assert b.fill < 0.65  # still recognisably a diamond
        assert abs((b.x1 - b.x0) - (a.x1 - a.x0)) < 0.12 * (a.x1 - a.x0)
        assert abs((b.y1 - b.y0) - (a.y1 - a.y0)) < 0.12 * (a.y1 - a.y0)


@pytest.mark.parametrize("name", ["fahrenheit_celsius", "sp_open_exit", "sum_loop"])
def test_framed_only_diagrams_use_exact_geometry(name: str) -> None:
    graph, lg = run_fixture(name)
    assert graph.boxes_detected
    assert quality_issues(lg) == []
