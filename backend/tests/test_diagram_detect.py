"""Tests for image-measured geometry (``app/core/diagram_detect.py``) and the
exact-box layout built on it. Offline: real sample images, no API calls.
The vision model's part (text, topology, marks) is supplied by hand.
"""

from pathlib import Path

import pytest

from app.core.diagram_detect import annotate, apply_detection, detect
from app.core.layout_boxes import layout_from_boxes
from app.models.diagram import (
    DiagramEdge,
    DiagramGraph,
    DiagramNode,
    LayoutedGraph,
)
from app.models.diagram import (
    PortSide as P,
)
from app.models.diagram import (
    ShapeType as T,
)

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
EUCLID = SAMPLES / "Blok Sxem1.jpeg"
LOOP = SAMPLES / "flowchart_a3.png"


def _e(s: str, t: str, label: str | None = None, port: P | None = None) -> DiagramEdge:
    return DiagramEdge(source_id=s, target_id=t, label=label, source_port=port)


def _euclid_graph() -> DiagramGraph:
    """Topology as the vision model returns it, marks as on the marked image."""
    spec = [
        ("n1", T.TERMINATOR, "Başlanğıc", 1),
        ("n2", T.INPUT_OUTPUT, "a,b", 2),
        ("n3", T.PROCESS, "c=a*b", 3),
        ("n4", T.DECISION, "a==b", 4),
        ("n5", T.DECISION, "a>b", 6),
        ("n6", T.PROCESS, "a=a-b", 9),
        ("n7", T.PROCESS, "b=b-a", 8),
        ("n8", T.PROCESS, "d=c/a", 5),
        ("n9", T.INPUT_OUTPUT, "d", 7),
        ("n10", T.TERMINATOR, "Son", 10),
    ]
    return DiagramGraph(
        nodes=[DiagramNode(id=i, shape_type=s, text=t, mark=m) for i, s, t, m in spec],
        edges=[
            _e("n1", "n2"),
            _e("n2", "n3"),
            _e("n3", "n4"),
            _e("n4", "n8", "Hə", P.RIGHT),
            _e("n4", "n5", "Yox", P.BOTTOM),
            _e("n5", "n6", "Hə", P.LEFT),
            _e("n5", "n7", "Yox", P.RIGHT),
            _e("n6", "n4"),
            _e("n7", "n4"),
            _e("n8", "n9"),
            _e("n9", "n10"),
        ],
    )


def _loop_graph() -> DiagramGraph:
    spec = [
        ("n1", T.TERMINATOR, "Başlanğıc", 1),
        ("n2", T.INPUT_OUTPUT, "n", 2),
        ("n3", T.PROCESS, "a = 3", 3),
        ("n4", T.DECISION, "a < 164", 4),
        ("n5", T.INPUT_OUTPUT, "n", 5),
        ("n6", T.TERMINATOR, "Son", 7),
        ("n7", T.PROCESS, "n = n - 8", 6),
        ("n8", T.PROCESS, "a = a + 16", 8),
    ]
    return DiagramGraph(
        nodes=[DiagramNode(id=i, shape_type=s, text=t, mark=m) for i, s, t, m in spec],
        edges=[
            _e("n1", "n2"),
            _e("n2", "n3"),
            _e("n3", "n4"),
            _e("n4", "n5", "Yox", P.LEFT),
            _e("n5", "n6"),
            _e("n4", "n7", "Hə", P.RIGHT),
            _e("n7", "n8"),
            _e("n8", "n4"),
        ],
    )


def quality_issues(lg: LayoutedGraph) -> list[str]:
    """Every visual defect a reader would notice: lines through symbols or
    labels, labels on symbols, lines on top of / crossing unrelated lines,
    diagonal segments, overlapping symbols."""
    issues: list[str] = []
    labels = []
    for e in lg.edges:
        if e.label and e.label_x is not None and e.label_y is not None:
            w = len(e.label) * 8 + 4
            x = e.label_x + 60 - w if e.label_align_right else e.label_x
            labels.append((e, (x, e.label_y, x + w, e.label_y + 18)))

    def hits(
        a: list[float], b: list[float], r: tuple[float, ...], pad: float = 0
    ) -> bool:
        return (
            min(a[0], b[0]) < r[2] - pad
            and max(a[0], b[0]) > r[0] + pad
            and min(a[1], b[1]) < r[3] - pad
            and max(a[1], b[1]) > r[1] + pad
        )

    def segs(e: DiagramEdge) -> list[tuple[list[float], list[float]]]:
        p = e.edge_path or []
        return list(zip(p, p[1:], strict=False))

    for e in lg.edges:
        name = f"{e.source_id}->{e.target_id}"
        for a, b in segs(e):
            if abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5:
                issues.append(f"diagonal {name}")
            for n in lg.nodes:
                if n.id not in (e.source_id, e.target_id) and hits(
                    a, b, (n.x, n.y, n.x + n.width, n.y + n.height)
                ):
                    issues.append(f"{name} through node {n.id}")
            for le, box in labels:
                if le is not e and hits(a, b, box, 1):
                    issues.append(f"{name} through label {le.label!r}")
    for le, box in labels:
        for n in lg.nodes:
            if hits(
                [box[0], box[1]],
                [box[2], box[3]],
                (n.x, n.y, n.x + n.width, n.y + n.height),
            ):
                issues.append(f"label {le.label!r} over node {n.id}")
    edges = lg.edges
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            if edges[i].target_id == edges[j].target_id:
                continue  # arrows into the same symbol may merge
            for a, b in segs(edges[i]):
                for c, d in segs(edges[j]):
                    va, vc = abs(a[0] - b[0]) < 0.5, abs(c[0] - d[0]) < 0.5
                    if va == vc:
                        k = 0 if va else 1
                        if abs(a[k] - c[k]) < 1:
                            lo = max(min(a[1 - k], b[1 - k]), min(c[1 - k], d[1 - k]))
                            hi = min(max(a[1 - k], b[1 - k]), max(c[1 - k], d[1 - k]))
                            if hi - lo > 1:
                                issues.append(f"overlap {i}/{j}")
                    else:
                        v, h = ((a, b), (c, d)) if va else ((c, d), (a, b))
                        x, y = v[0][0], h[0][1]
                        if (
                            min(h[0][0], h[1][0]) + 0.5
                            < x
                            < max(h[0][0], h[1][0]) - 0.5
                            and min(v[0][1], v[1][1]) + 0.5
                            < y
                            < max(v[0][1], v[1][1]) - 0.5
                        ):
                            issues.append(f"cross {i}/{j}")
    nodes = lg.nodes
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a, b = nodes[i], nodes[j]
            if (
                a.x < b.x + b.width
                and b.x < a.x + a.width
                and a.y < b.y + b.height
                and b.y < a.y + a.height
            ):
                issues.append(f"nodes overlap {a.id} {b.id}")
    return issues


@pytest.mark.parametrize("path,count", [(EUCLID, 10), (LOOP, 8)])
def test_detects_every_symbol(path: Path, count: int) -> None:
    det = detect(path.read_bytes())
    assert det is not None
    assert len(det.shapes) == count


def test_shape_fill_tells_diamonds_apart() -> None:
    det = detect(LOOP.read_bytes())
    assert det is not None
    diamonds = [s for s in det.shapes if s.fill < 0.65]
    assert len(diamonds) == 1  # "a < 164"


def test_finds_the_original_loop_line() -> None:
    det = detect(LOOP.read_bytes())
    assert det is not None
    w = det.width
    # The loop-back runs down the far right of the image.
    assert any(
        abs(x0 - x1) < 2 and x0 > 0.9 * w and abs(y1 - y0) > 0.3 * det.height
        for x0, y0, x1, y1 in det.segments
    )


def test_annotate_returns_png() -> None:
    data = EUCLID.read_bytes()
    det = detect(data)
    assert det is not None
    assert annotate(data, det)[:8] == b"\x89PNG\r\n\x1a\n"


def test_partial_marks_keep_model_boxes() -> None:
    g = _euclid_graph()
    g.nodes[0].mark = None
    g.nodes[0].box_2d = [100, 300, 150, 450]
    det = detect(EUCLID.read_bytes())
    assert det is not None
    apply_detection(g, det)
    assert not g.boxes_detected
    assert g.nodes[0].box_2d == [100, 300, 150, 450]
    assert g.nodes[1].box_2d is not None


def test_duplicate_marks_are_not_trusted() -> None:
    g = _euclid_graph()
    g.nodes[1].mark = g.nodes[0].mark
    det = detect(EUCLID.read_bytes())
    assert det is not None
    apply_detection(g, det)
    assert not g.boxes_detected


@pytest.mark.parametrize("path,make", [(EUCLID, _euclid_graph), (LOOP, _loop_graph)])
def test_real_samples_render_without_any_overlap(path: Path, make) -> None:  # type: ignore[no-untyped-def]
    g = make()
    det = detect(path.read_bytes())
    assert det is not None
    apply_detection(g, det)
    assert g.boxes_detected and g.guide_segments
    lg = layout_from_boxes(g)
    assert lg is not None
    assert quality_issues(lg) == []


def test_loop_follows_the_original_outer_route() -> None:
    g = _loop_graph()
    det = detect(LOOP.read_bytes())
    assert det is not None
    apply_detection(g, det)
    lg = layout_from_boxes(g)
    assert lg is not None
    loop = next(e for e in lg.edges if e.source_id == "n8")
    rightmost = max(n.x + n.width for n in lg.nodes)
    # Like the source: around the right of every symbol, into the top.
    assert max(p[0] for p in loop.edge_path or []) > rightmost
    decision = next(n for n in lg.nodes if n.id == "n4")
    assert loop.edge_path is not None
    assert loop.edge_path[-1][1] == pytest.approx(decision.y, abs=1)
