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
from app.core.vision.debug_dump import DebugDump
from app.core.vision.postprocess import (
    attach_geometry,
    parse_diagram_graph,
    parse_vision_analysis,
    pick_better_answer,
    topology_problems,
)
from app.models.document import ContentType, VisionAnalysis, VisionResult

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

_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

_PROMPT = """\
You are given a scanned image from an Azerbaijani school informatics test.
FIRST decide what it is, then extract it into the matching JSON payload.

Set "content_type" to:
- "diagram" — an algorithm flowchart (boxes/diamonds/ovals joined by arrows).
- "text_content" — textual content with NO flow arrows, e.g. a matching \
question made of two labeled lists (a numbered list and a lettered list).

If content_type is "diagram", fill "diagram" with {nodes, edges, title, \
title_below}:
- One node per flowchart symbol. shape_type is one of:
  "terminator" (oval/stadium/rounded box, e.g. Başlanğıc / Son / START),
  "process" (plain rectangle, e.g. assignments),
  "decision" (diamond, conditions),
  "input_output" (parallelogram: straight, slanted left and right sides; \
e.g. "... daxil edilməsi", READ),
  "document" (rectangle whose BOTTOM edge is a wave instead of a straight \
line; textbooks draw print/output blocks this way, e.g. "... çap \
edilməsi", "Çap etməli: ..."). Look at the bottom edge: wavy = "document", \
slanted sides = "input_output", straight = "process".
- "text" is the label EXACTLY as written, in Azerbaijani. Do not translate, \
do not compute, do not complete. When the label is on several lines inside \
the symbol, keep each line and join them with "\\n".
- A symbol that is EMPTY in the image gets "text": "" (empty string). If \
some characters are unreadable, write only what is readable. Never invent \
text that is not printed in the image (e.g. never fill an empty box with \
the formula you expect there).
- Old scans often print "ə" as "›" or a similar broken glyph (e.g. \
"daxil edilm›si" means "daxil edilməsi", "h›" means "hə"); write the \
intended Azerbaijani letter.
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
- A drawing may be a fragment without "Başlanğıc"/"Son": then its first \
symbol has no incoming arrow — that is correct, do not add one.
- For edges leaving a DECISION (diamond) node: set "source_port" to the \
side the arrow exits from in the image — "left", "right", "bottom", or \
"top". Look at the image carefully. Leave it null for non-decision nodes \
or when unsure.
- For every node set "box_2d" to the symbol's bounding box in the image as \
[ymin, xmin, ymax, xmax], integers normalised to 0-1000 (relative to the \
image height / width). Measure carefully: the output layout reproduces the \
original arrangement from these boxes.
- Node ids "n1", "n2", ... in reading order. Include loop-back arrows.
- A flowchart always has arrows, so "edges" must not be empty.
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


class GeminiVisionProvider(VisionProvider):
    """Diagram analysis backed by the Google Gemini API (JSON mode)."""

    def __init__(self) -> None:
        settings = get_settings()
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

    def _retry_topology(
        self, contents: list[str | genai_types.Part], problems: list[str]
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
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[*contents[:-1], str(contents[-1]) + fix],  # type: ignore[arg-type]
                config=self._config,
            )
        except Exception:  # noqa: BLE001 - keep the first answer
            logger.exception("Topology retry failed")
            return None, ""
        raw = response.text or ""
        analysis = parse_vision_analysis(raw)
        if analysis is None or analysis.content_type != ContentType.DIAGRAM:
            return None, raw
        return analysis, raw

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        """Classify the image and extract it in ONE call.

        Method name kept for the interface; despite the name it also
        handles TEXT_CONTENT images. Returns a ``VisionResult`` whose
        ``content_type`` selects ``diagram_graph`` or ``text_content``.
        """
        prompt = _PROMPT
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
        dump.detection(detection)
        if marked is not None:
            dump.binary("marked.png", marked)
        dump.text("prompt.txt", prompt)

        last_error: Exception | None = None
        raw_output = ""
        analysis: VisionAnalysis | None = None
        for attempt in range(1 + _MAX_RETRIES):
            try:
                response = self._client.models.generate_content(
                    model=self._model_name,
                    # mypy: the SDK's contents union spells Image as
                    # PIL.Image.Image; our narrower list is fine at runtime.
                    contents=contents,  # type: ignore[arg-type]
                    config=self._config,
                )
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
