"""Gemini implementation of the vision provider interface.

This is the only module in the codebase allowed to import the Gemini SDK
(``google-genai``).
"""

import logging
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.config import get_settings
from app.core.diagram_detect import Detection, annotate, detect
from app.core.vision.base import VisionProvider
from app.core.vision.budget import VisionBudget, VisionBudgetExceededError
from app.core.vision.debug_dump import DebugDump
from app.core.vision.postprocess import (
    attach_geometry,
    filter_page,
    page_topology_problems,
    parse_diagram_graph,
    parse_vision_analysis,
    pick_better_answer,
    topology_problems,
)
from app.models.document import ContentType, VisionAnalysis, VisionResult
from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun

__all__ = [
    "GeminiVisionProvider",
    "parse_diagram_graph",
    "parse_vision_analysis",
    "topology_problems",
]

logger = logging.getLogger(__name__)

_MARKS_PROMPT = """
A SECOND image is attached: the same picture with the automatically \
detected flowchart symbols outlined in red, each with a red number tag at \
its top-left corner. For every node set "mark" to the number of the red \
outline around that symbol. If a symbol has no red outline, set "mark" to \
null (and still give its box_2d). Never give two nodes the same mark. The \
marks are only for identification; read the text from the FIRST image.
"""

_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 2.0
_REQUEST_TIMEOUT_MS = 60_000
# A dense page answers with far more text than one diagram.
_PAGE_REQUEST_TIMEOUT_MS = 120_000
# Page mode sometimes calls a flowchart a "figure" (seen live: a plain
# flowchart photo came back as a figure in 2 of 3 runs). When the page answer
# has no flowchart but the local detector found this many symbols, the old
# single-diagram prompt is asked once for the whole image.
_RESCUE_MIN_SHAPES = 4
_RESCUE_MIN_LABELLED_NODES = 3
_RESCUE_OVERLAP = 0.4

_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

_PROMPT_HEAD = """\
You are given a scanned image from an Azerbaijani school informatics test.
FIRST decide what it is, then extract it into the matching JSON payload.

Set "content_type" to:
- "diagram" — an algorithm flowchart (boxes/diamonds/ovals joined by arrows).
- "text_content" — textual content with NO flow arrows, e.g. a matching \
question made of two labeled lists (a numbered list and a lettered list).

If content_type is "diagram", fill "diagram" with {nodes, edges, title, \
title_below}:
"""

# The node/arrow rules, shared by the single-diagram prompt and the page prompt.
_FLOWCHART_RULES = """\
- One node per flowchart symbol. shape_type is one of:
  "terminator" (oval/stadium/rounded box, e.g. Başlanğıc / Son / START),
  "process" (plain rectangle, e.g. assignments),
  "decision" (diamond: FOUR corners, a condition such as "X <= Y"),
  "preparation" (HEXAGON: flat top and bottom, pointed left and right \
ends — a loop header such as "i=2,N" or "i=1,10"; never call a hexagon a \
decision),
  "input_output" (parallelogram: straight, slanted left and right sides; \
e.g. "... daxil edilməsi", READ),
  "document" (rectangle whose BOTTOM edge is a wave instead of a straight \
line; textbooks draw print/output blocks this way, e.g. "... çap \
edilməsi", "Çap etməli: ..."). Look at the bottom edge: wavy = "document", \
slanted sides = "input_output", straight = "process".
- "text" is the label EXACTLY as written, in Azerbaijani. Do not translate, \
do not compute, do not complete. When the label is on several lines inside \
the symbol, keep each line and join them with "\\n".
- Subscripts and superscripts in "text" (and in "title") are written as \
_{...} and ^{...}: "X_{1}, X_{2}, ... X_{20}", "10^{3}". Digits printed on the normal \
baseline stay plain.
- When a node's label is a mathematical formula PRINTED with a stacked \
fraction (numerator above a bar, denominator below), a root sign, or \
raised/lowered characters, ALSO give it in "latex". A slash written on \
one line ("5/9 * (F - 32)") is NOT a stacked fraction: then "latex" is \
null and "text" keeps the slash. Use only \\frac, \\sqrt, _, ^, \\text, \
\\cdot, \\le, \\ge, \\ne, \\ldots: "x = \\frac{-b}{2a}". Keep "text" as a readable \
one-line version ("x = -b / 2a"). "latex" is null for ordinary labels.
- A symbol that is EMPTY in the image gets "text": "" (empty string). If \
some characters are unreadable, write only what is readable. Never invent \
text that is not printed in the image (e.g. never fill an empty box with \
the formula you expect there).
- Old scans often print "ə" as "›" or a similar broken glyph (e.g. \
"daxil edilm›si" means "daxil edilməsi", "h›" means "hə", \
"qiym› tl› rinin" means "qiymətlərinin"); write the intended Azerbaijani \
letter and keep EVERY syllable — a broken glyph replaces one letter, it \
never removes a syllable.
- Numbers printed in the margin beside the symbols (step/row numbers \
1, 2, 3, ...) are NOT part of the flowchart: ignore them.
- One edge per arrow, in the arrow's direction. Branch labels on arrows \
(e.g. "hə", "yox") go in that edge's "label", written as printed; null \
when the arrow has none.
- An arrow that ends ON ANOTHER ARROW'S LINE (not on a symbol) joins that \
line: its target_id is the symbol that line finally points to. Follow the \
line to its arrowhead; never connect it to the symbol the line comes from.
- An arrow whose head points into EMPTY SPACE (no symbol at its tip: the \
algorithm continues outside the picture) ends at a node with shape_type \
"exit", text "", mark null and box_2d a small box around the arrowhead tip. \
Arrows that join one such open line share one "exit" node. Never connect \
these arrows to some other symbol, and never add an arrow that is not drawn.
- A formula or text that is part of the flow (arrows point to or from it) \
but is drawn WITHOUT any outline is still a node: shape_type "process", \
"framed": false, mark null, and box_2d tightly around its text. Every \
symbol with an outline has "framed": true.
- A drawing may be a fragment without "Başlanğıc"/"Son": then its first \
symbol has no incoming arrow — that is correct, do not add one.
- For edges leaving a DECISION (diamond) or PREPARATION (hexagon) node: \
set "source_port" to the side the arrow exits from in the image — "left", \
"right", "bottom", or "top". Look at the image carefully. Leave it null for \
other nodes or when unsure. A hexagon's two arrows (into the loop body and \
out of the loop) often carry no label: then "label" is null.
- For every node set "box_2d" to the symbol's bounding box in the image as \
[ymin, xmin, ymax, xmax], integers normalised to 0-1000 (relative to the \
image height / width). Measure carefully: the output layout reproduces the \
original arrangement from these boxes.
- Node ids "n1", "n2", ... in reading order. Include loop-back arrows.
- A flowchart always has arrows, so "edges" must not be empty.
"""

_PROMPT_TAIL = """\
- "title" is a heading or caption line that belongs to the flowchart but \
sits OUTSIDE its symbols and arrows — either above it (e.g. "Problem 2: \
...", "Blok-sxem nəyi hesablayır?") or below it (e.g. "Sxem 3. ... \
alqoritminin blok-sxemi."). Preserve it EXACTLY, including any formula in \
it (write subscripts inline, e.g. "S = x1 + x2 + ... + x20"). Set \
"title_below" to true when that line is BELOW the flowchart, false when it \
is above. Leave "title" null if there is no such line — never invent one.
Leave "text_content" null in this case.

If content_type is "text_content", fill "text_content" with {heading, lists}:
- "heading" is the instruction line above the two lists, if the image has \
one (e.g. "87. Uyğunluğu müəyyən edin."). Preserve it EXACTLY, including \
the leading question number if present. Leave it null if the image has no \
such line — never invent heading text that isn't actually in the image.
- "lists" is an array of independent labeled lists in left-to-right order \
(e.g. the numbered list first, then the lettered list).
- Each list has "lines"; each line has "runs"; each run has "text" and \
"subscript" (a boolean).
- Preserve every character EXACTLY, including the label prefix ("1. ", \
"a. "). Split numeral-base notation into runs: "65" (subscript false) then \
"10" (subscript true) for 65 with a subscript 10. Only the base digits are \
subscript.
Leave "diagram" null in this case.
"""

_PROMPT = _PROMPT_HEAD + _FLOWCHART_RULES + _PROMPT_TAIL

_PAGE_HEAD = """\
You are given a PHOTOGRAPH or SCAN of a PAGE from an Azerbaijani school \
informatics test (a book page, a phone screenshot of a PDF, or a crop of one). \
Describe the whole page as "blocks" in READING ORDER.

Set "content_type" to "page" and fill "page" with {"blocks": [...]}. Leave \
"diagram" and "text_content" null.

Every block has "kind" and "box_2d" ([ymin, xmin, ymax, xmax], integers \
normalised to 0-1000 relative to the image height / width, tight around the \
block). The kinds:
- "text": printed text. Give "style": "heading" for a question line that \
starts with its number ("118. ..."), "option" for an answer line that starts \
with a letter ("A) ...", "B) ..."), otherwise "paragraph". Give "runs": each \
run has "text", "subscript" and "superscript" (booleans; true only for \
characters printed lowered or raised, e.g. the base in 65 with a small 10, or \
the y in x^y). One block per question stem (join its wrapped lines with a \
single space) and ONE block per answer option: never put several options \
("A) ... B) ...") into one block, even when they are printed on one line or \
in columns. When a question is followed by equations, formulas or conditions \
printed on separate lines, give each printed line its own block (style \
"paragraph"); only the wrapped lines of an ordinary sentence are joined.
- "table": a printed grid; "rows" is a list of rows, each a list of cell texts.
- "flowchart": an algorithm flowchart (symbols joined by arrows). Fill \
"diagram" with {nodes, edges} following the flowchart rules below, leave its \
"title" null and "title_below" false: the question text above or below the \
chart is its own "text" block. "box_2d" is the box around the whole chart.
- "figure": any other picture that cannot be written as text: a drawing, \
graph, plot, pattern matrix, geometric shape, photo, a picture used as an \
answer option, and anything laid out as a diagram rather than running text \
(for example long-division or column-arithmetic layouts). "box_2d" is tight \
around it, including the letters that label its options. Never also copy a \
figure's inner text or labels into text blocks. A flowchart (symbols joined \
by arrows) is NEVER a figure: it is always kind "flowchart".

Text rules:
- Preserve every character EXACTLY as printed: Azerbaijani letters (ə, ı, ş, \
ç, ğ, ö, ü, İ), digits and mathematical signs (< > ≤ ≥ ≠ = := + - × ÷ ...). \
Do not translate, compute, complete or correct anything.
- A fraction printed stacked (numerator above a bar) is written inline with a \
slash: "1/3".
- Old scans print "ə" as "›" or a similar broken glyph: write the intended \
letter and keep every syllable.

Page rules:
- Reading order is top to bottom. On a page with two columns, give the whole \
LEFT column first, then the whole RIGHT column.
- Include only the printed content of the main page. IGNORE and never output: \
faint, washed-out or mirrored text showing through from the back of the \
paper; handwriting and pen marks; text cut off by the edge of the image (half \
lines at the border); phone status bars, app toolbars and page counters \
(e.g. "9/60"); watermarks; running headers and footers; page numbers, \
including a lone number in a small box at the top of the page; content of \
the neighbouring page. If you are unsure whether something is \
printed text or a ghost, skip it.

Flowchart rules (for blocks of kind "flowchart"):
"""

_PAGE_PROMPT = _PAGE_HEAD + _FLOWCHART_RULES


def _mime_type(image: bytes) -> str:
    """Best-effort MIME type from the file signature."""
    if image[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


def _is_transient(exc: Exception) -> bool:
    """Return True for errors worth retrying (rate limits, 5xx, network)."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _TRANSIENT_HTTP_CODES
    return isinstance(exc, ConnectionError | TimeoutError)


def _overlap(a: list[int], b: list[int]) -> float:
    """Part of box ``b`` inside box ``a`` (both as ymin, xmin, ymax, xmax)."""
    ymin, xmin = max(a[0], b[0]), max(a[1], b[1])
    ymax, xmax = min(a[2], b[2]), min(a[3], b[3])
    if ymax <= ymin or xmax <= xmin:
        return 0.0
    area = (b[2] - b[0]) * (b[3] - b[1])
    return (ymax - ymin) * (xmax - xmin) / area if area > 0 else 0.0


class GeminiVisionProvider(VisionProvider):
    """Diagram analysis backed by the Google Gemini API (JSON mode)."""

    def __init__(self, budget: VisionBudget | None = None) -> None:
        settings = get_settings()
        self._budget = budget
        if not settings.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY is not configured; cannot create Gemini provider."
            )
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model_name = settings.gemini_model
        self._config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VisionAnalysis,
            http_options=genai_types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
        )
        # Never set a temperature here: measured live, an explicit 0 or 0.3 made
        # the request hit the 504 deadline (3 minutes) where the default answers
        # in about 5 seconds.
        self._page_config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VisionAnalysis,
            http_options=genai_types.HttpOptions(timeout=_PAGE_REQUEST_TIMEOUT_MS),
            max_output_tokens=settings.page_max_output_tokens,
        )

    def _note_usage(self, response: object, kind: str) -> None:
        """Log and count the real token use of one request (numbers only)."""
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                tokens = [
                    getattr(usage, name, None)
                    for name in (
                        "prompt_token_count",
                        "candidates_token_count",
                        "thoughts_token_count",
                    )
                ]
                logger.info(
                    "gemini_usage kind=%s in=%s out=%s thoughts=%s", kind, *tokens
                )
                if self._budget is not None:
                    self._budget.add_usage(
                        *(t if isinstance(t, int) else None for t in tokens)
                    )
            for candidate in getattr(response, "candidates", None) or []:
                if "MAX_TOKENS" in str(getattr(candidate, "finish_reason", "")):
                    logger.warning(
                        "Gemini answer cut at the output token ceiling (kind=%s)",
                        kind,
                    )
        except Exception:  # noqa: BLE001 - logging must never break a request
            logger.debug("Could not read token usage", exc_info=True)

    def _consume_budget(self) -> None:
        """Count one Gemini request against the job's ceiling, if there is one."""
        if self._budget is not None:
            self._budget.consume()

    def _retry_topology(
        self,
        contents: list[str | genai_types.Part],
        problems: list[str],
        expect: ContentType = ContentType.DIAGRAM,
        config: genai_types.GenerateContentConfig | None = None,
    ) -> tuple[VisionAnalysis | None, str]:
        """Single corrective call listing what was structurally wrong.

        Returns ``(analysis or None, raw response text)``.
        """
        fix = (
            "\nYour previous answer for this image was incomplete:\n- "
            + "\n- ".join(problems)
            + "\nLook at every arrow again, especially arrows that go back up "
            "to an earlier symbol and arrows that end in empty space (those "
            'end at an "exit" node). Answer again with exactly the arrows '
            "that are drawn — do not invent arrows to satisfy these rules."
        )
        try:
            self._consume_budget()
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[*contents[:-1], str(contents[-1]) + fix],  # type: ignore[arg-type]
                config=config or self._config,
            )
        except VisionBudgetExceededError:
            raise
        except Exception:  # noqa: BLE001 - keep the first answer
            logger.exception("Topology retry failed")
            return None, ""
        self._note_usage(response, "topology_retry")
        raw = response.text or ""
        analysis = parse_vision_analysis(raw)
        if analysis is None or analysis.content_type != expect:
            return None, raw
        return analysis, raw

    def _ask_again(
        self,
        contents: list[str | genai_types.Part],
        config: genai_types.GenerateContentConfig,
    ) -> tuple[VisionAnalysis | None, str]:
        """Repeat the same request once (an answer without any text block)."""
        try:
            self._consume_budget()
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=contents,  # type: ignore[arg-type]
                config=config,
            )
        except VisionBudgetExceededError:
            raise
        except Exception:  # noqa: BLE001 - keep the first answer
            logger.exception("Repeat of the page request failed")
            return None, ""
        self._note_usage(response, "page_repeat")
        raw = response.text or ""
        analysis = parse_vision_analysis(raw)
        if analysis is None or analysis.content_type != ContentType.PAGE:
            return None, raw
        return analysis, raw

    def _rescue_flowchart(
        self,
        page: PageAnalysis,
        contents: list[str | genai_types.Part],
        marked: bool,
    ) -> PageAnalysis:
        """Ask the proven single-diagram prompt for the whole image.

        Used only when the page answer has no flowchart block although the
        detector saw several symbols. The diagram replaces the figure block it
        covers (or is inserted by position); a caption the model put in the
        diagram's ``title`` becomes a text block. Anything doubtful leaves the
        page as it was.
        """
        prompt = _PROMPT + (_MARKS_PROMPT if marked else "")
        try:
            self._consume_budget()
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[*contents[:-1], prompt],  # type: ignore[arg-type]
                config=self._config,
            )
        except VisionBudgetExceededError:
            raise
        except Exception:  # noqa: BLE001 - keep the page as it is
            logger.exception("Flowchart rescue failed")
            return page
        self._note_usage(response, "flowchart_rescue")
        analysis = parse_vision_analysis(response.text or "")
        if (
            analysis is None
            or analysis.content_type != ContentType.DIAGRAM
            or analysis.diagram is None
        ):
            return page
        graph = analysis.diagram
        labelled = sum(1 for n in graph.nodes if n.text.strip())
        if (
            labelled < _RESCUE_MIN_LABELLED_NODES
            or len(graph.edges) < 2
            or len(topology_problems(graph)) > 1
        ):
            return page
        boxes = [n.box_2d for n in graph.nodes if n.box_2d and len(n.box_2d) == 4]
        union = (
            [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
            if boxes
            else None
        )
        title, title_below = graph.title, graph.title_below
        graph.title, graph.title_below = None, False
        chart = PageBlock(kind=BlockKind.FLOWCHART, diagram=graph, box_2d=union)

        blocks = list(page.blocks)
        index = len(blocks)
        if union is not None:
            index = next(
                (
                    i
                    for i, b in enumerate(blocks)
                    if b.box_2d and b.box_2d[0] > union[0]
                ),
                len(blocks),
            )
            covered = [
                i
                for i, b in enumerate(blocks)
                if b.kind == BlockKind.FIGURE
                and b.box_2d is not None
                and _overlap(b.box_2d, union) >= _RESCUE_OVERLAP
            ]
            if covered:
                index = covered[0]
                blocks = [b for i, b in enumerate(blocks) if i not in covered]
        blocks.insert(index, chart)
        if title and not any(
            " ".join(title.split()) == " ".join(b.text.split())
            for b in blocks
            if b.kind == BlockKind.TEXT
        ):
            caption = PageBlock(
                kind=BlockKind.TEXT,
                style=TextStyle.PARAGRAPH,
                runs=[TextRun(text=title)],
            )
            blocks.insert(index + 1 if title_below else index, caption)
        return PageAnalysis(blocks=blocks)

    def _finish_page(
        self,
        analysis: VisionAnalysis,
        raw_output: str,
        image: bytes,
        detection: Detection | None,
        marked: bool,
        contents: list[str | genai_types.Part],
        config: genai_types.GenerateContentConfig,
        dump: DebugDump,
        meta: dict[str, object],
    ) -> VisionResult:
        """Check, filter and finish a whole-page answer."""
        page = analysis.page
        assert page is not None
        problems = page_topology_problems(page)
        meta["problems"] = problems
        if problems:
            # Same single corrective re-ask as for a lone diagram.
            logger.warning("Page flowchart incomplete: %s", "; ".join(problems))
            retry, retry_raw = self._retry_topology(
                contents, problems, expect=ContentType.PAGE, config=config
            )
            dump.text("retry.json", retry_raw)
            kept = (
                retry is not None
                and retry.page is not None
                and len(page_topology_problems(retry.page)) < len(problems)
            )
            meta["retry_kept"] = kept
            if kept and retry is not None and retry.page is not None:
                page, raw_output = retry.page, retry_raw
        readable = (BlockKind.TEXT, BlockKind.FLOWCHART)
        if not any(b.kind in readable for b in page.blocks):
            # An answer with only figures/tables and neither text nor a
            # flowchart is almost always incomplete (seen live on a dense
            # page); one repeat is cheap.
            again, again_raw = self._ask_again(contents, config)
            if (
                again is not None
                and again.page is not None
                and any(b.kind in readable for b in again.page.blocks)
            ):
                page, raw_output = again.page, again_raw
                meta["empty_text_repeat"] = True
        if (
            not any(b.kind == BlockKind.FLOWCHART for b in page.blocks)
            and detection is not None
            and len(detection.shapes) >= _RESCUE_MIN_SHAPES
        ):
            page = self._rescue_flowchart(page, contents, marked)
            meta["rescue_tried"] = True
        if get_settings().ghost_filter_enabled:
            page = filter_page(page, image)
        for block in page.blocks:
            if block.diagram is not None:
                attach_geometry(block.diagram, image, detection, marked=marked)
        dump.text("page.json", page.model_dump_json(indent=2))
        dump.json("meta.json", meta)
        return VisionResult(
            raw_model_output=raw_output,
            content_type=ContentType.PAGE,
            page=page,
            confidence_note=f"Generated by {self._model_name}. Read as a whole page.",
        )

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        """Classify the image and extract it in ONE call.

        Method name kept for the interface; despite the name it also
        handles TEXT_CONTENT images. Returns a ``VisionResult`` whose
        ``content_type`` selects ``diagram_graph``, ``text_content`` or, with
        ``page=True``, the ``page`` blocks of a whole page.
        """
        prompt = _PAGE_PROMPT if page else _PROMPT
        dump = DebugDump(image)
        detection: Detection | None = None
        try:
            detection = detect(image)
        except Exception:  # noqa: BLE001 - geometry is an optional refinement
            logger.exception("Shape detection failed; using model boxes only")
        marked: bytes | None = None
        if detection is not None and len(detection.shapes) >= 2:
            marked = annotate(image, detection)
            prompt += _MARKS_PROMPT
        if context:
            prompt += (
                "\nSurrounding text from the source document (for context "
                f"only, do not add it to the output):\n{context}"
            )
        contents: list[str | genai_types.Part] = [
            genai_types.Part.from_bytes(data=image, mime_type=_mime_type(image)),
        ]
        if marked is not None:
            contents.append(
                genai_types.Part.from_bytes(data=marked, mime_type="image/png")
            )
        contents.append(prompt)
        config = self._page_config if page else self._config
        dump.detection(detection)
        if marked is not None:
            dump.binary("marked.png", marked)
        dump.text("prompt.txt", prompt)

        last_error: Exception | None = None
        raw_output = ""
        analysis: VisionAnalysis | None = None
        for attempt in range(1 + _MAX_RETRIES):
            try:
                self._consume_budget()
                response = self._client.models.generate_content(
                    model=self._model_name,
                    # mypy: the SDK's contents union spells Image as
                    # PIL.Image.Image; our narrower list is fine at runtime.
                    contents=contents,  # type: ignore[arg-type]
                    config=config,
                )
                self._note_usage(response, "page" if page else "image")
                raw_output = response.text or ""
                analysis = parse_vision_analysis(raw_output)
                if analysis is not None:
                    break
                # Invalid/incomplete JSON is treated like a transient
                # failure: the model often produces a complete payload on a
                # second attempt.
                logger.warning(
                    "Model returned invalid VisionAnalysis JSON (attempt %d/%d)",
                    attempt + 1,
                    1 + _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SECONDS)
            except VisionBudgetExceededError:
                raise
            except Exception as exc:  # noqa: BLE001 - must not crash pipeline
                last_error = exc
                if not _is_transient(exc):
                    logger.exception("Gemini call failed permanently")
                    return VisionResult(error=f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "Transient Gemini error (attempt %d/%d): %s",
                    attempt + 1,
                    1 + _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
        else:
            if last_error is not None:
                return VisionResult(
                    raw_model_output=raw_output,
                    error=(
                        f"Gemini call failed after {1 + _MAX_RETRIES} attempts: "
                        f"{type(last_error).__name__}: {last_error}"
                    ),
                )
            return VisionResult(
                raw_model_output=raw_output,
                error=(
                    "Model response was not a valid VisionAnalysis JSON "
                    f"after {1 + _MAX_RETRIES} attempts."
                ),
            )

        dump.text("response.json", raw_output)
        meta: dict[str, object] = {"model": self._model_name, "context": context}
        if analysis.content_type == ContentType.PAGE and analysis.page is not None:
            return self._finish_page(
                analysis,
                raw_output,
                image,
                detection,
                marked is not None,
                contents,
                config,
                dump,
                meta,
            )
        if analysis.content_type == ContentType.DIAGRAM and analysis.diagram:
            problems = topology_problems(analysis.diagram)
            meta["problems"] = problems
            if problems:
                # One targeted re-ask (only when the answer is provably
                # incomplete, e.g. a loop-back arrow was left out).
                logger.warning("Diagram topology incomplete: %s", "; ".join(problems))
                retry, retry_raw = self._retry_topology(contents, problems)
                dump.text("retry.json", retry_raw)
                analysis, kept = pick_better_answer(analysis, retry)
                meta["retry_kept"] = kept
                if retry is not None and retry.diagram is not None:
                    meta["retry_problems"] = topology_problems(retry.diagram)
                if kept:
                    raw_output = retry_raw
                    still = meta["retry_problems"]
                    if still:
                        logger.warning("Topology still incomplete: %s", still)
        dump.json("meta.json", meta)

        note = f"Generated by {self._model_name}."
        if analysis.content_type == ContentType.DIAGRAM:
            if analysis.diagram is not None:
                attach_geometry(
                    analysis.diagram, image, detection, marked=marked is not None
                )
                dump.text("graph.json", analysis.diagram.model_dump_json(indent=2))
            return VisionResult(
                raw_model_output=raw_output,
                content_type=ContentType.DIAGRAM,
                diagram_graph=analysis.diagram,
                confidence_note=f"{note} Geometry computed downstream.",
            )
        return VisionResult(
            raw_model_output=raw_output,
            content_type=ContentType.TEXT_CONTENT,
            text_content=analysis.text_content,
            confidence_note=f"{note} Extracted as text content, not a diagram.",
        )
