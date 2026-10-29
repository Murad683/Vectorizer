"""Re-run detection -> layout -> render for a diagram WITHOUT any API call.

Three input modes (run from backend/, e.g. inside the backend container):

    # 1. A debug dump written by a real vision call (DEBUG_DUMP_DIR=...):
    python scripts/replay_diagram.py data/debug_dumps/20261029-124250-1a2b3c4d

    # 2. A hand-built ideal answer from tests/diagram_fixtures.py:
    python scripts/replay_diagram.py --fixture quadratic_equation
    python scripts/replay_diagram.py --fixture all

    # 3. Any image + a DiagramGraph JSON (marks and/or box_2d):
    python scripts/replay_diagram.py --image x.png --graph graph.json

For each input it prints the topology problems and ``quality_issues()``
and writes, into ``--out`` (default: ``<dump>/replay`` or
``data/replay/<name>``): ``preview.png`` (layout geometry), ``compare.png``
(source | result), ``diagram.docx`` and ``diagram.pptx`` (the real
assembler path, so titles/captions are included). Exit code 1 if any
input has quality issues.

The CURRENT detector always runs; for dumps the model's marks are remapped
from the shapes it saw (``detection.json``) onto the current shapes, so
detector improvements can be replayed against old model answers.
"""

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.core.assembly.docx_assembler import assemble_docx  # noqa: E402
from app.core.assembly.pptx_assembler import assemble_pptx  # noqa: E402
from app.core.diagram_detect import DetectedShape, Detection, detect  # noqa: E402
from app.core.diagram_quality import quality_issues  # noqa: E402
from app.core.layout import layout_graph  # noqa: E402
from app.core.render.png_preview import render_png  # noqa: E402
from app.core.vision.base import VisionProvider  # noqa: E402
from app.core.vision.postprocess import (  # noqa: E402
    attach_geometry,
    parse_vision_analysis,
    pick_better_answer,
    topology_problems,
)
from app.models.diagram import DiagramGraph  # noqa: E402
from app.models.document import (  # noqa: E402
    ClassifiedElement,
    ContentType,
    ElementKind,
    ElementType,
    ExtractedElement,
    VisionResult,
)


class _FixedProvider(VisionProvider):
    """Returns a precomputed result: the assemblers run unchanged."""

    def __init__(self, graph: DiagramGraph) -> None:
        self._graph = graph

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        return VisionResult(
            content_type=ContentType.DIAGRAM,
            diagram_graph=self._graph.model_copy(deep=True),
        )


def _iou(a: DetectedShape, b: DetectedShape) -> float:
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    inter = ix * iy
    area = (a.x1 - a.x0) * (a.y1 - a.y0) + (b.x1 - b.x0) * (b.y1 - b.y0) - inter
    return inter / area if area > 0 else 0.0


def _remap_marks(graph: DiagramGraph, old: Detection, new: Detection) -> None:
    """Translate marks given against ``old`` shapes into ``new`` numbering."""
    for node in graph.nodes:
        if node.mark is None or not 1 <= node.mark <= len(old.shapes):
            continue
        src = old.shapes[node.mark - 1]
        best = max(
            range(len(new.shapes)),
            key=lambda i: _iou(src, new.shapes[i]),
            default=None,
        )
        if best is None or _iou(src, new.shapes[best]) < 0.5:
            node.mark = None
        else:
            node.mark = best + 1


def _load_detection(path: Path) -> Detection:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Detection(
        width=data["width"],
        height=data["height"],
        shapes=[DetectedShape(**s) for s in data["shapes"]],
        segments=[tuple(s) for s in data["segments"]],  # type: ignore[misc]
    )


def _from_dump(folder: Path) -> tuple[bytes, DiagramGraph, Detection | None]:
    image_path = next(folder.glob("image.*"))
    first = parse_vision_analysis(
        (folder / "response.json").read_text(encoding="utf-8")
    )
    if first is None or first.diagram is None:
        raise SystemExit(f"{folder}: response.json holds no usable diagram")
    retry_file = folder / "retry.json"
    retry = (
        parse_vision_analysis(retry_file.read_text(encoding="utf-8"))
        if retry_file.exists()
        else None
    )
    chosen, kept = pick_better_answer(first, retry)
    print(f"  answer used: {'retry' if kept else 'first'}")
    assert chosen.diagram is not None
    seen = folder / "detection.json"
    return (
        image_path.read_bytes(),
        chosen.diagram,
        _load_detection(seen) if seen.exists() else None,
    )


def _compare(image: bytes, preview: bytes) -> bytes:
    """Source and result side by side, scaled to the same height."""
    a = Image.open(io.BytesIO(image)).convert("RGB")
    b = Image.open(io.BytesIO(preview)).convert("RGB")
    height = max(a.height, b.height, 600)
    a = a.resize((round(a.width * height / a.height), height))
    b = b.resize((round(b.width * height / b.height), height))
    out = Image.new("RGB", (a.width + b.width + 20, height), "white")
    out.paste(a, (0, 0))
    out.paste(b, (a.width + 20, 0))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def replay(
    name: str,
    image: bytes,
    graph: DiagramGraph,
    seen: Detection | None,
    out: Path,
    simulate: bool = False,
) -> list[str]:
    """Run the offline half of the pipeline; return the quality issues."""
    print(f"== {name}")
    detection = detect(image)
    if detection is None:
        raise SystemExit(f"{name}: image cannot be decoded")
    if simulate:
        from tests.diagram_fixtures import simulate_marks

        simulate_marks(graph, detection)
    elif seen is not None:
        _remap_marks(graph, seen, detection)
    attach_geometry(graph, image, detection, marked=True)
    matched = sum(n.mark is not None for n in graph.nodes)
    print(
        f"  detected {len(detection.shapes)} shapes, {len(detection.segments)} "
        f"guides; marks {matched}/{len(graph.nodes)}; "
        f"exact={graph.boxes_detected}"
    )
    for p in topology_problems(graph):
        print(f"  topology: {p}")
    layouted = layout_graph(graph.model_copy(deep=True))
    issues = quality_issues(layouted)
    for issue in issues:
        print(f"  ISSUE: {issue}")
    if not issues:
        print("  quality: OK (0 issues)")

    out.mkdir(parents=True, exist_ok=True)
    preview = render_png(layouted)
    (out / "preview.png").write_bytes(preview)
    (out / "compare.png").write_bytes(_compare(image, preview))
    (out / "graph.json").write_text(graph.model_dump_json(indent=2), encoding="utf-8")
    element = ClassifiedElement(
        element=ExtractedElement(
            source_page=1, element_kind=ElementKind.IMAGE, raw_content=image
        ),
        element_type=ElementType.DIAGRAM,
        rule="replay",
    )
    provider = _FixedProvider(graph)
    assemble_docx([element], vision_provider=provider).save(str(out / "diagram.docx"))
    assemble_pptx([element], vision_provider=provider).save(str(out / "diagram.pptx"))
    print(f"  wrote {out}")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dump", nargs="?", help="debug dump folder")
    parser.add_argument("--fixture", help="fixture name from tests/diagram_fixtures")
    parser.add_argument("--image", help="image file (with --graph)")
    parser.add_argument("--graph", help="DiagramGraph JSON (with --image)")
    parser.add_argument("--out", help="output folder")
    args = parser.parse_args()

    backend = Path(__file__).resolve().parents[1]
    failed = False
    if args.fixture:
        from tests.diagram_fixtures import FIXTURES, SAMPLES

        names = list(FIXTURES) if args.fixture == "all" else [args.fixture]
        for name in names:
            file, make = FIXTURES[name]
            out = Path(args.out) / name if args.out else backend / "data/replay" / name
            image = (SAMPLES / file).read_bytes()
            failed |= bool(replay(name, image, make(), None, out, simulate=True))
    elif args.image and args.graph:
        image = Path(args.image).read_bytes()
        graph = DiagramGraph.model_validate_json(
            Path(args.graph).read_text(encoding="utf-8")
        )
        name = Path(args.image).stem
        out = Path(args.out) if args.out else backend / "data/replay" / name
        failed = bool(replay(name, image, graph, None, out))
    elif args.dump:
        folder = Path(args.dump)
        image, graph, seen = _from_dump(folder)
        out = Path(args.out) if args.out else folder / "replay"
        failed = bool(replay(folder.name, image, graph, seen, out))
    else:
        parser.error("give a dump folder, --fixture NAME|all, or --image + --graph")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
