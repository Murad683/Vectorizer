"""Gemini implementation of the vision provider interface.

This is the only module in the codebase allowed to import the Gemini SDK
(``google-genai``).
"""

import logging
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import ValidationError

from app.config import get_settings
from app.core.vision.base import VisionProvider
from app.models.diagram import DiagramGraph
from app.models.document import ContentType, VisionAnalysis, VisionResult

logger = logging.getLogger(__name__)

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

If content_type is "diagram", fill "diagram" with {nodes, edges, title}:
- One node per flowchart symbol. shape_type is one of:
  "terminator" (oval/stadium, e.g. Başlanğıc / Son),
  "process" (rectangle, e.g. assignments),
  "decision" (diamond, conditions),
  "input_output" (parallelogram, input or output of a variable).
- "text" is the label EXACTLY as written, in Azerbaijani. Do not translate.
- One edge per arrow, in the arrow's direction. Branch labels on arrows \
(e.g. "Hə", "Yox") go in that edge's "label"; null when the arrow has none.
- For edges leaving a DECISION (diamond) node: set "source_port" to the \
side the arrow exits from in the image — "left", "right", "bottom", or \
"top". Look at the image carefully. Leave it null for non-decision nodes \
or when unsure.
- Node ids "n1", "n2", ... in reading order. Include loop-back arrows.
- A flowchart always has arrows, so "edges" must not be empty.
- "title" is a heading/question line positioned ABOVE the flowchart itself \
(e.g. "Blok-sxem nəyi hesablayır?" or "Problem 2: ..."), if the image has \
one. This is NOT any node's own label — it sits outside the flowchart's \
boxes/arrows entirely. Preserve it EXACTLY. Leave it null if the image has \
no such line — never invent a title that isn't actually in the image.
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


def _is_transient(exc: Exception) -> bool:
    """Return True for errors worth retrying (rate limits, 5xx, network)."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _TRANSIENT_HTTP_CODES
    return isinstance(exc, ConnectionError | TimeoutError)


def _normalise_graph(graph: DiagramGraph) -> DiagramGraph | None:
    """Normalise edge labels; return None if the graph has no nodes."""
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

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        """Classify the image and extract it in ONE call.

        Method name kept for the interface; despite the name it also
        handles TEXT_CONTENT images. Returns a ``VisionResult`` whose
        ``content_type`` selects ``diagram_graph`` or ``text_content``.
        """
        prompt = _PROMPT
        if context:
            prompt += (
                "\nSurrounding text from the source document (for context "
                f"only, do not add it to the output):\n{context}"
            )
        contents: list[str | genai_types.Part] = [
            genai_types.Part.from_bytes(data=image, mime_type="image/png"),
            prompt,
        ]

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

        note = f"Generated by {self._model_name}."
        if analysis.content_type == ContentType.DIAGRAM:
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
