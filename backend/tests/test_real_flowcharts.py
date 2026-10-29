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
        ("factorial_preparation.png", 7),
        ("xy_loop_photo.png", 7),
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


@pytest.mark.parametrize(
    "name",
    [
        "fahrenheit_celsius",
        "sp_open_exit",
        "sum_loop",
        "factorial_preparation",
        "xy_loop_photo",
    ],
)
def test_framed_only_diagrams_use_exact_geometry(name: str) -> None:
    graph, _ = run_fixture(name)
    assert graph.boxes_detected


@pytest.mark.parametrize("name", list(FIXTURES))
def test_no_visual_defects(name: str) -> None:
    _, lg = run_fixture(name)
    assert quality_issues(lg) == []


def test_quadratic_routes_like_the_source() -> None:
    """Both print blocks reach "Son" down the LEFT and merge there; the
    "hə" branch of D > 0 leaves to the right, outside D = 0's branch — the
    Word result used to send 8 -> Son right, across the D > 0 line."""
    _, lg = run_fixture("quadratic_equation")
    son = lg.node_by_id("n11")
    for src in ("n6", "n8"):
        e = next(e for e in lg.edges if e.source_id == src and e.target_id == "n11")
        assert e.edge_path is not None
        assert min(p[0] for p in e.edge_path) < son.x  # runs down the left
        end = e.edge_path[-1]
        # ...and stops with its own arrowhead on the line into "Son".
        assert end[0] == pytest.approx(son.x + son.width / 2, abs=0.5)
        assert end[1] < son.y - 1
    outer = next(e for e in lg.edges if e.source_id == "n4" and e.target_id == "n9")
    inner = next(e for e in lg.edges if e.source_id == "n5" and e.target_id == "n7")
    assert outer.edge_path is not None and inner.edge_path is not None
    d4 = lg.node_by_id("n4")
    assert outer.edge_path[0][0] == pytest.approx(d4.x + d4.width)  # right tip
    assert max(p[0] for p in outer.edge_path) > max(p[0] for p in inner.edge_path)


def test_loop_lands_on_the_connector_into_the_decision() -> None:
    """``P < 10`` "yox" goes up the left and enters ``S < 0`` from the top,
    where the source draws it landing on the incoming connector."""
    _, lg = run_fixture("sp_open_exit")
    loop = next(e for e in lg.edges if e.source_id == "n5" and e.target_id == "n2")
    target = lg.node_by_id("n2")
    assert loop.edge_path is not None
    end_x, end_y = loop.edge_path[-1]
    assert end_x == pytest.approx(target.x + target.width / 2, abs=0.5)
    assert end_y < target.y - 1  # its arrowhead sits on the connector
    assert min(p[0] for p in loop.edge_path) < target.x
