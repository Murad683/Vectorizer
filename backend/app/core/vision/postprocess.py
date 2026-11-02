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
from app.models.page import BlockKind, PageAnalysis, PageBlock

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
    if analysis.content_type == ContentType.PAGE:
        if analysis.page is None:
            return None
        analysis.page = clean_page(analysis.page)
        return analysis if analysis.page.blocks else None
    if analysis.content_type == ContentType.DIAGRAM:
        if analysis.diagram is None or _normalise_graph(analysis.diagram) is None:
            return None
        return analysis
    if analysis.text_content is None or not any(
        lst.lines for lst in analysis.text_content.lists
    ):
        return None
    return analysis


# Ghost-text filter (page mode). Measured on a real photographed page: printed
# text has an ink/paper ratio of 0.7-0.8, the mirrored text showing through the
# paper 0.1-0.15, in shadow as well as in light. Handwriting (about 0.3, but
# thin and sparse) cannot be told apart reliably: the prompt covers it.
_MIN_INK_RATIO = 0.35
# A box that lands on blank paper (ratio ~0) is a misplaced box, not ghost
# text: such blocks are kept, only clearly faint ink is dropped.
_BLANK_RATIO = 0.03
# Models place boxes a little off, so the ink is measured in a slightly larger
# window (of 1000): a real line then still finds its dark text.
_WINDOW_DY = 25
_WINDOW_DX = 10
_EDGE_MARGIN = 6  # of 1000: a block this close to the image edge is cut off
_EDGE_MAX_CHARS = 60  # only short fragments are dropped for touching the edge


def _valid_box(box: list[int] | None) -> list[int] | None:
    """Clamp a [ymin, xmin, ymax, xmax] box to 0-1000; None if unusable."""
    if box is None or len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = (max(0, min(1000, int(v))) for v in box)
    if ymax <= ymin or xmax <= xmin:
        return None
    return [ymin, xmin, ymax, xmax]


def clean_page(page: PageAnalysis) -> PageAnalysis:
    """Drop blocks that carry nothing usable; keep reading order.

    A flowchart without arrows cannot be drawn, so it is kept as a figure
    (cut out of the image) when it has a box; a figure without a box cannot
    be cut out and is dropped.
    """
    kept: list[PageBlock] = []
    for block in page.blocks:
        block.box_2d = _valid_box(block.box_2d)
        if block.kind == BlockKind.TEXT:
            block.runs = [r for r in block.runs or [] if r.text]
            if block.text.strip():
                kept.append(block)
        elif block.kind == BlockKind.TABLE:
            rows = [r for r in block.rows or [] if any(c.strip() for c in r)]
            if rows:
                block.rows = rows
                kept.append(block)
        elif block.kind == BlockKind.FLOWCHART:
            graph = _normalise_graph(block.diagram) if block.diagram else None
            if graph is not None and graph.edges:
                block.diagram = graph
                kept.append(block)
            elif block.box_2d is not None:
                block.kind = BlockKind.FIGURE
                block.diagram = None
                kept.append(block)
        elif block.box_2d is not None:  # FIGURE
            kept.append(block)
    return PageAnalysis(blocks=kept)


def page_topology_problems(page: PageAnalysis) -> list[str]:
    """Structural problems of every flowchart block of the page."""
    problems: list[str] = []
    for block in page.blocks:
        if block.kind == BlockKind.FLOWCHART and block.diagram is not None:
            problems.extend(topology_problems(block.diagram))
    return problems


def ink_ratio(gray: Image.Image, box: list[int]) -> float:
    """How strongly dark ink stands out from the paper inside ``box``.

    ``(paper - ink) / paper`` from the 50th and 3rd brightness percentiles of
    the region; 0 for an empty region.
    """
    width, height = gray.size
    ymin, xmin, ymax, xmax = box
    left = int(xmin / 1000 * width)
    top = int(ymin / 1000 * height)
    region = gray.crop(
        (
            left,
            top,
            max(left + 1, int(xmax / 1000 * width)),
            max(top + 1, int(ymax / 1000 * height)),
        )
    )
    histogram = region.histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0

    def percentile(fraction: float) -> int:
        seen = 0
        for value, count in enumerate(histogram):
            seen += count
            if seen >= total * fraction:
                return value
        return 255

    paper = percentile(0.5)
    if paper <= 0:
        return 0.0
    return (paper - percentile(0.03)) / paper


def _inflate(box: list[int]) -> list[int]:
    ymin, xmin, ymax, xmax = box
    return [
        max(0, ymin - _WINDOW_DY),
        max(0, xmin - _WINDOW_DX),
        min(1000, ymax + _WINDOW_DY),
        min(1000, xmax + _WINDOW_DX),
    ]


def filter_page(page: PageAnalysis, image: bytes) -> PageAnalysis:
    """Remove text the model should have skipped: ghost, handwriting, cut-off.

    Deterministic second line of defence behind the prompt. Only text and
    table blocks are tested; if the image cannot be read the page is
    returned unchanged.
    """
    try:
        with Image.open(io.BytesIO(image)) as img:
            gray = img.convert("L")
    except Exception:  # noqa: BLE001 - keep the model answer as it is
        logger.warning("Could not read the image for the ghost-text filter")
        return page
    kept: list[PageBlock] = []
    for block in page.blocks:
        box = block.box_2d
        if block.kind in (BlockKind.TEXT, BlockKind.TABLE) and box is not None:
            ymin, xmin, ymax, xmax = box
            touches_edge = (
                xmin <= _EDGE_MARGIN
                or xmax >= 1000 - _EDGE_MARGIN
                or ymin <= _EDGE_MARGIN
                or ymax >= 1000 - _EDGE_MARGIN
            )
            if touches_edge and len(block.text) < _EDGE_MAX_CHARS:
                logger.info("Dropped a cut-off fragment: %r", block.text[:40])
                continue
            ratio = ink_ratio(gray, _inflate(box))
            if _BLANK_RATIO <= ratio < _MIN_INK_RATIO:
                logger.info("Dropped faint text (%.2f): %r", ratio, block.text[:40])
                continue
        kept.append(block)
    return PageAnalysis(blocks=kept)


def topology_problems(graph: DiagramGraph) -> list[str]:
    """Structural rules every flowchart obeys; violations mean the model
    missed or invented arrows (most often a loop-back arrow).

    - process / input_output / document: exactly 1 outgoing arrow;
    - decision: exactly 2; preparation (loop header): 1, or 2 to enter the
      loop body and to leave the loop (often unlabelled);
    - exit (open arrow end): none, and at least 1 in;
    - every symbol is entered, except the entry point: a start terminator,
      or — in a fragment drawn without terminators — the first symbol;
    - a fragment drawn without terminators and without an open arrow end
      may stop at exactly one block that has no outgoing arrow;
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
    # Likewise a fragment may simply stop: drawn without any terminator and
    # without an open arrow end, ONE entered block without an outgoing arrow
    # is its last step (seen live: a test question ending at the print
    # block "S"). Two such blocks still mean a missing arrow, and a drawing
    # that has "Başlanğıc" is expected to reach "Son".
    has_end = any(n.shape_type.value in ("terminator", "exit") for n in graph.nodes)
    sinks = [
        n.id
        for n in graph.nodes
        if n.shape_type.value in ("process", "input_output", "document")
        and outs[n.id] == 0
        and ins[n.id] > 0
    ]
    last = sinks[0] if not has_end and len(sinks) == 1 else None
    problems: list[str] = []
    for n in graph.nodes:
        label = f'"{n.text}" ({n.id})'
        kind = n.shape_type.value
        if (
            kind in ("process", "input_output", "document")
            and outs[n.id] != 1
            and n.id != last
        ):
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
