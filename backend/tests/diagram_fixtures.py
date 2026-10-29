"""Hand-built "ideal model answers" for the real sample flowcharts.

Each fixture is what a perfect vision model should return for the image:
correct text (Azerbaijani, line breaks kept), shape types and topology,
plus each symbol's box (measured by hand from the image, in pixels) so
that ``simulate_marks`` can play the model's set-of-mark answer against
whatever the current detector finds. Used by the tests and by
``scripts/replay_diagram.py --fixture NAME`` — no API calls anywhere.
"""

from collections.abc import Callable
from pathlib import Path

from app.core.diagram_detect import Detection, detect
from app.core.layout import layout_graph
from app.core.vision.postprocess import attach_geometry
from app.models.diagram import DiagramEdge, DiagramGraph, DiagramNode, LayoutedGraph
from app.models.diagram import PortSide as P
from app.models.diagram import ShapeType as T

SAMPLES = Path(__file__).resolve().parents[2] / "samples"

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in image pixels


def _graph(
    size: tuple[int, int],
    nodes: list[tuple[str, T, str, Box]],
    edges: list[DiagramEdge],
    **extra: object,
) -> DiagramGraph:
    w, h = size
    return DiagramGraph(
        nodes=[
            DiagramNode(
                id=nid,
                shape_type=shape,
                text=text,
                box_2d=[
                    round(y0 / h * 1000),
                    round(x0 / w * 1000),
                    round(y1 / h * 1000),
                    round(x1 / w * 1000),
                ],
            )
            for nid, shape, text, (x0, y0, x1, y1) in nodes
        ],
        edges=edges,
        **extra,  # type: ignore[arg-type]
    )


def _e(s: str, t: str, label: str | None = None, port: P | None = None) -> DiagramEdge:
    return DiagramEdge(source_id=s, target_id=t, label=label, source_port=port)


def quadratic_equation() -> DiagramGraph:
    """``samples/quadratic_equation.png`` (505x614): roots of ax²+bx+c.

    Blocks 7 and 9 are formulas drawn WITHOUT a frame; block 3 is empty in
    the source; 6, 8 and 10 are print (document) symbols. Row numbers 2-11
    on the left are not part of the flowchart.
    """
    return _graph(
        (505, 614),
        [
            ("n1", T.TERMINATOR, "Başlanğıc", (108, 1, 301, 38)),
            (
                "n2",
                T.INPUT_OUTPUT,
                "a, b, c əmsallarının\ndaxil edilməsi",
                (110, 49, 299, 86),
            ),
            ("n3", T.PROCESS, "", (108, 97, 301, 132)),
            ("n4", T.DECISION, "D > 0", (113, 143, 296, 193)),
            ("n5", T.DECISION, "D = 0", (107, 215, 302, 262)),
            (
                "n6",
                T.DOCUMENT,
                'Çap etməli: "Tənliyin həlli yoxdur"',
                (120, 274, 301, 311),
            ),
            ("n7", T.PROCESS, "x = -b / 2a", (130, 320, 292, 345)),
            (
                "n8",
                T.DOCUMENT,
                "x-in qiymətlərinin çap edilməsi",
                (120, 365, 301, 402),
            ),
            (
                "n9",
                T.PROCESS,
                "x1 = (-b + √D) / 2a, x2 = (-b - √D) / 2a",
                (117, 420, 301, 467),
            ),
            (
                "n10",
                T.DOCUMENT,
                "x1 və x2-nin qiymətlərinin\nçap edilməsi",
                (120, 478, 301, 528),
            ),
            ("n11", T.TERMINATOR, "Son", (120, 562, 301, 587)),
        ],
        [
            _e("n1", "n2"),
            _e("n2", "n3"),
            _e("n3", "n4"),
            _e("n4", "n9", "hə", P.RIGHT),
            _e("n4", "n5", "yox", P.BOTTOM),
            _e("n5", "n7", "hə", P.RIGHT),
            _e("n5", "n6", "yox", P.BOTTOM),
            _e("n6", "n11"),
            _e("n7", "n8"),
            _e("n8", "n11"),
            _e("n9", "n10"),
            _e("n10", "n11"),
        ],
    )


def fahrenheit_celsius() -> DiagramGraph:
    """``samples/fahrenheit_celsius.png`` (616x463): a straight chain."""
    return _graph(
        (616, 463),
        [
            ("n1", T.TERMINATOR, "START", (245, 106, 384, 134)),
            ("n2", T.INPUT_OUTPUT, "READ F", (206, 174, 416, 213)),
            ("n3", T.PROCESS, "C = 5/9 * (F - 32)", (218, 247, 407, 286)),
            ("n4", T.INPUT_OUTPUT, "PRINT C", (225, 313, 408, 357)),
            ("n5", T.TERMINATOR, "END", (257, 391, 376, 416)),
        ],
        [_e("n1", "n2"), _e("n2", "n3"), _e("n3", "n4"), _e("n4", "n5")],
        title="Problem 2: Convert temperature Fahrenheit to Celsius.",
    )


def sp_open_exit() -> DiagramGraph:
    """``samples/sp_open_exit.png`` (581x398): no terminators; both
    ``P = P * k`` blocks run into one open exit arrow at the bottom right,
    and the "yox" loop of ``P < 10`` lands on the connector into ``S < 0``.
    """
    return _graph(
        (581, 398),
        [
            ("n1", T.PROCESS, "S = 11\nP = 2000", (99, 11, 275, 71)),
            ("n2", T.DECISION, "S < 0", (102, 96, 270, 166)),
            ("n3", T.PROCESS, "P = P * 2", (312, 101, 488, 161)),
            ("n4", T.PROCESS, "S = S - 3\nP = P // 10", (101, 194, 277, 255)),
            ("n5", T.DECISION, "P < 10", (99, 280, 271, 351)),
            ("n6", T.PROCESS, "P = P * 3", (313, 282, 489, 342)),
        ],
        [
            _e("n1", "n2"),
            _e("n2", "n3", "hə", P.RIGHT),
            _e("n2", "n4", "yox", P.BOTTOM),
            _e("n4", "n5"),
            _e("n5", "n6", "hə", P.RIGHT),
            _e("n5", "n2", "yox", P.LEFT),
        ],
    )


def sum_loop() -> DiagramGraph:
    """``samples/sum_loop.jpg`` (394x504): S = x1 + ... + x20, caption BELOW."""
    return _graph(
        (394, 504),
        [
            ("n1", T.TERMINATOR, "Başlanğıc", (104, 33, 264, 65)),
            (
                "n2",
                T.INPUT_OUTPUT,
                "X1, X2, ... X20 ədədlərinin\ndaxil edilməsi",
                (95, 75, 273, 118),
            ),
            ("n3", T.PROCESS, "S = 0", (103, 129, 265, 151)),
            ("n4", T.PROCESS, "I = 1", (103, 160, 268, 183)),
            ("n5", T.PROCESS, "S = S + X(I)", (103, 203, 268, 225)),
            ("n6", T.PROCESS, "I = I + 1", (103, 235, 265, 279)),
            ("n7", T.DECISION, "I > 20", (108, 289, 249, 332)),
            (
                "n8",
                T.DOCUMENT,
                "S-in qiymətinin çap edilməsi",
                (104, 353, 275, 385),
            ),
            ("n9", T.TERMINATOR, "Son", (108, 396, 268, 428)),
        ],
        [
            _e("n1", "n2"),
            _e("n2", "n3"),
            _e("n3", "n4"),
            _e("n4", "n5"),
            _e("n5", "n6"),
            _e("n6", "n7"),
            _e("n7", "n5", "yox", P.RIGHT),
            _e("n7", "n8", "hə", P.BOTTOM),
            _e("n8", "n9"),
        ],
        title=(
            "Sxem 3. S = x1 + x2 + ... + x20 cəminin hesablanması "
            "alqoritminin blok-sxemi."
        ),
        title_below=True,
    )


FIXTURES: dict[str, tuple[str, Callable[[], DiagramGraph]]] = {
    "quadratic_equation": ("quadratic_equation.png", quadratic_equation),
    "fahrenheit_celsius": ("fahrenheit_celsius.png", fahrenheit_celsius),
    "sp_open_exit": ("sp_open_exit.png", sp_open_exit),
    "sum_loop": ("sum_loop.jpg", sum_loop),
}


def simulate_marks(graph: DiagramGraph, detection: Detection) -> None:
    """Play a perfect model's set-of-mark answer: each node gets the number
    of the detected shape that contains its (hand-measured) box centre, or
    ``None`` when the detector found no symbol there (e.g. a frameless
    formula)."""
    w, h = float(detection.width), float(detection.height)
    used: set[int] = set()
    for node in graph.nodes:
        node.mark = None
        if node.box_2d is None:
            continue
        ymin, xmin, ymax, xmax = node.box_2d
        cx = (xmin + xmax) / 2000 * w
        cy = (ymin + ymax) / 2000 * h
        for i, s in enumerate(detection.shapes, start=1):
            if i not in used and s.x0 <= cx <= s.x1 and s.y0 <= cy <= s.y1:
                node.mark = i
                used.add(i)
                break


def run_fixture(name: str) -> tuple[DiagramGraph, LayoutedGraph]:
    """Detection + simulated marks + layout for one fixture, offline."""
    file, make = FIXTURES[name]
    image = (SAMPLES / file).read_bytes()
    detection = detect(image)
    assert detection is not None
    graph = make()
    simulate_marks(graph, detection)
    attach_geometry(graph, image, detection, marked=True)
    return graph, layout_graph(graph.model_copy(deep=True))
