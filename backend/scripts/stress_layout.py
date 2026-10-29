"""Offline stress test for detection + box layout + routing (no API calls).

Each topology of ``tests/test_no_diagonal.ALL_TOPOLOGIES`` is drawn by
Graphviz as a PNG "scan" (real outlines, orthogonal arrows). Then the
production pipeline runs on it: ``detect`` -> perfect marks simulated from
Graphviz's node positions -> ``apply_detection`` -> ``layout_graph`` ->
``quality_issues``. Prints one line per topology and a summary.

Usage (from backend/, inside the backend container):
    python scripts/stress_layout.py [--out data/stress]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.diagram_detect import Detection, detect  # noqa: E402
from app.core.diagram_quality import quality_issues  # noqa: E402
from app.core.layout import layout_graph  # noqa: E402
from app.core.render.png_preview import render_png  # noqa: E402
from app.core.vision.postprocess import attach_geometry  # noqa: E402
from app.models.diagram import DiagramGraph, LayoutedGraph, ShapeType  # noqa: E402
from tests.diagram_fixtures import simulate_marks  # noqa: E402
from tests.test_no_diagonal import ALL_TOPOLOGIES  # noqa: E402

_DPI = 96
_PAD_PT = 36.0  # graph "pad" = 0.5 in
_GV_SHAPE = {
    ShapeType.TERMINATOR: 'shape=box style="rounded"',
    ShapeType.PROCESS: "shape=box",
    ShapeType.DECISION: "shape=diamond",
    ShapeType.INPUT_OUTPUT: "shape=parallelogram",
    ShapeType.DOCUMENT: "shape=box",
    ShapeType.EXIT: "shape=point",
}


def _dot(graph: DiagramGraph) -> str:
    lines = [
        "digraph G {",
        f'graph [splines=ortho, nodesep=0.6, ranksep=0.6, pad="0.5", dpi={_DPI}];',
        'node [fontname="DejaVu Sans", fontsize=11, penwidth=1.5];',
        "edge [penwidth=1.5, arrowsize=0.8];",
    ]
    for n in graph.nodes:
        text = n.text.replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'"{n.id}" [{_GV_SHAPE[n.shape_type]}, label="{text}"];')
    for e in graph.edges:
        label = f' [xlabel="{e.label}"]' if e.label else ""
        lines.append(f'"{e.source_id}" -> "{e.target_id}"{label};')
    lines.append("}")
    return "\n".join(lines)


def _draw(graph: DiagramGraph) -> tuple[bytes, dict[str, tuple[float, ...]]]:
    """PNG bytes + each node's pixel box (x0, y0, x1, y1) in that PNG."""
    src = _dot(graph).encode("utf-8")
    png = subprocess.run(["dot", "-Tpng"], input=src, capture_output=True, check=True)
    lay = subprocess.run(["dot", "-Tjson0"], input=src, capture_output=True, check=True)
    data = json.loads(lay.stdout)
    bb_h = float(data["bb"].split(",")[3])
    k = _DPI / 72.0
    boxes: dict[str, tuple[float, ...]] = {}
    for obj in data.get("objects", []):
        x, y = (float(v) for v in obj["pos"].split(","))
        w, h = float(obj["width"]) * 72, float(obj["height"]) * 72
        cx, cy = (x + _PAD_PT) * k, (bb_h - y + _PAD_PT) * k
        boxes[obj["name"]] = (
            cx - w * k / 2,
            cy - h * k / 2,
            cx + w * k / 2,
            cy + h * k / 2,
        )
    return png.stdout, boxes


def run_one(template: DiagramGraph) -> tuple[int, int, bool, list[str]]:
    """Draw, detect, lay out one topology.

    Returns ``(detected shapes, symbols, exact geometry, quality issues)``.
    """
    lg, graph, det, _ = _run(template)
    symbols = sum(n.shape_type != ShapeType.EXIT for n in graph.nodes)
    return len(det.shapes), symbols, graph.boxes_detected, quality_issues(lg)


def _run(
    template: DiagramGraph,
) -> tuple[LayoutedGraph, DiagramGraph, Detection, bytes]:
    graph = template.model_copy(deep=True)
    image, px = _draw(graph)
    det = detect(image)
    assert det is not None
    for n in graph.nodes:
        x0, y0, x1, y1 = px[n.id]
        n.box_2d = [
            round(y0 / det.height * 1000),
            round(x0 / det.width * 1000),
            round(y1 / det.height * 1000),
            round(x1 / det.width * 1000),
        ]
    simulate_marks(graph, det)
    attach_geometry(graph, image, det, marked=True)
    return layout_graph(graph.model_copy(deep=True)), graph, det, image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", help="write PNG previews here")
    args = parser.parse_args()
    clean = 0
    total = 0
    for name, template in ALL_TOPOLOGIES:
        if len(template.nodes) < 2:
            continue
        total += 1
        lg, graph, det, image = _run(template)
        issues = quality_issues(lg)
        clean += not issues
        symbols = sum(n.shape_type != ShapeType.EXIT for n in graph.nodes)
        print(
            f"{name:28s} shapes {len(det.shapes):2d}/{symbols:2d} "
            f"exact={str(graph.boxes_detected):5s} issues={len(issues)} "
            + "; ".join(issues[:4])
        )
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{name}_src.png").write_bytes(image)
            (out / f"{name}_out.png").write_bytes(render_png(lg))
    print(f"\n{clean}/{total} topologies with zero quality issues")


if __name__ == "__main__":
    main()
