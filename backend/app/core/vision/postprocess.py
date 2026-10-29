"""Provider-independent handling of a vision model's answer.

Everything that happens to a model response AFTER the API call lives here
(parsing, structural checks, merging in the measured geometry), so the
offline replay tool (``scripts/replay_diagram.py``) runs exactly the same
code as the live pipeline without importing any vision SDK.
"""

import io
import logging

from PIL import Image
from pydantic import ValidationError

from app.core.diagram_detect import Detection, apply_detection
from app.core.rich_text import LatexError, latex_markup
from app.models.diagram import DiagramGraph
from app.models.document import ContentType, VisionAnalysis

logger = logging.getLogger(__name__)


def _normalise_graph(graph: DiagramGraph) -> DiagramGraph | None:
    """Normalise labels; return None if the graph has no nodes.

    A formula node's ``text`` is the readable fallback; models sometimes put
    LaTeX there too (``\\sqrt{D}``), which would surface verbatim wherever
    the text is used. It is rebuilt from ``latex`` (sub/superscripts kept as
    markup). Unparseable ``latex`` is dropped so the text is rendered.
    """
    for node in graph.nodes:
        if not node.latex:
            continue
        try:
            linear = latex_markup(node.latex)
        except LatexError:
            node.latex = None
            continue
        if "\\" in node.text:
            node.text = linear
    for edge in graph.edges:
        if edge.label is not None and not edge.label.strip():
            edge.label = None
    if not graph.nodes:
        return None
    return graph


def parse_diagram_graph(raw_output: str) -> DiagramGraph | None:
    """Validate a bare DiagramGraph JSON response, or None.

    Retained for direct-graph callers/tests; the envelope path uses
    ``parse_vision_analysis``.
    """
    try:
        graph = DiagramGraph.model_validate_json(raw_output)
    except ValidationError:
        return None
    return _normalise_graph(graph)


def parse_vision_analysis(raw_output: str) -> VisionAnalysis | None:
    """Validate a VisionAnalysis envelope and its selected payload, or None.

    A response is usable only when the payload matching ``content_type``
    is present and non-degenerate: a diagram with nodes, or text content
    with at least one non-empty list.
    """
    try:
        analysis = VisionAnalysis.model_validate_json(raw_output)
    except ValidationError:
        return None
    if analysis.content_type == ContentType.DIAGRAM:
        if analysis.diagram is None or _normalise_graph(analysis.diagram) is None:
            return None
        return analysis
    if analysis.text_content is None or not any(
        lst.lines for lst in analysis.text_content.lists
    ):
        return None
    return analysis


def topology_problems(graph: DiagramGraph) -> list[str]:
    """Structural rules every flowchart obeys; violations mean the model
    missed or invented arrows (most often a loop-back arrow).

    - process / input_output / document: exactly 1 outgoing arrow;
    - decision: exactly 2; preparation (loop header): 1, or 2 to enter the
      loop body and to leave the loop (often unlabelled);
    - exit (open arrow end): none, and at least 1 in;
    - every symbol is entered, except the entry point: a start terminator,
      or — in a fragment drawn without terminators — the first symbol;
    - no arrow from a symbol to itself (seen when a model "fixed" a missing
      arrow by inventing one).
    """
    outs: dict[str, int] = {n.id: 0 for n in graph.nodes}
    ins: dict[str, int] = {n.id: 0 for n in graph.nodes}
    for e in graph.edges:
        if e.source_id in outs:
            outs[e.source_id] += 1
        if e.target_id in ins:
            ins[e.target_id] += 1
    has_start = any(
        n.shape_type.value == "terminator" and ins[n.id] == 0 and outs[n.id] > 0
        for n in graph.nodes
    )
    # Without a start terminator the drawing is a fragment; its entry is
    # the first symbol in reading order (ids follow reading order).
    entry = graph.nodes[0].id if graph.nodes and not has_start else None
    problems: list[str] = []
    for n in graph.nodes:
        label = f'"{n.text}" ({n.id})'
        kind = n.shape_type.value
        if kind in ("process", "input_output", "document") and outs[n.id] != 1:
            problems.append(
                f"{label} is a {kind} symbol with {outs[n.id]} outgoing arrows; "
                "it must have exactly 1 (check loop-back arrows and arrows "
                "that end in empty space)"
            )
        elif kind == "decision" and outs[n.id] != 2:
            problems.append(
                f"{label} is a decision with {outs[n.id]} outgoing arrows; "
                "it must have exactly 2"
            )
        elif kind == "preparation" and outs[n.id] not in (1, 2):
            problems.append(
                f"{label} is a loop header (preparation) with {outs[n.id]} "
                "outgoing arrows; it must have 1 or 2 (into the loop body and "
                "out of the loop)"
            )
        elif kind == "terminator" and outs[n.id] + ins[n.id] == 0:
            problems.append(f"{label} has no arrows at all")
        elif kind == "exit" and (outs[n.id] or not ins[n.id]):
            problems.append(
                f"{label} is an open arrow end; it must have incoming arrows "
                "and no outgoing ones"
            )
        if kind not in ("terminator", "exit") and ins[n.id] == 0 and n.id != entry:
            problems.append(f"{label} has no incoming arrow")
    for e in graph.edges:
        if e.source_id == e.target_id:
            problems.append(f"an arrow goes from {e.source_id} to itself")
    return problems


def pick_better_answer(
    first: VisionAnalysis, retry: VisionAnalysis | None
) -> tuple[VisionAnalysis, bool]:
    """Keep the corrective retry only if it is structurally better.

    Returns ``(chosen, retry_kept)``.
    """
    if retry is None or retry.diagram is None or first.diagram is None:
        return first, False
    if len(topology_problems(retry.diagram)) < len(topology_problems(first.diagram)):
        return retry, True
    return first, False


def attach_geometry(
    graph: DiagramGraph, image: bytes, detection: Detection | None, marked: bool
) -> None:
    """Merge the measured geometry (or at least the image size) into ``graph``.

    ``marked`` says whether the model saw the set-of-mark image, i.e.
    whether its ``mark`` values refer to ``detection``'s shapes.
    """
    if detection is not None and marked:
        apply_detection(graph, detection)
        return
    # box_2d is normalised; the layout needs the aspect ratio.
    try:
        with Image.open(io.BytesIO(image)) as img:
            graph.image_width = img.width
            graph.image_height = img.height
    except Exception:  # noqa: BLE001 - boxes then fall back to Graphviz
        logger.warning("Could not read image size for box layout")
