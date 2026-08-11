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
from app.models.document import VisionResult

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 2.0
_REQUEST_TIMEOUT_MS = 60_000

_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

_PROMPT = """\
You are given a scanned image of an algorithm flowchart from an Azerbaijani \
school informatics test. Extract its structure as JSON.

Rules:
- One node per flowchart symbol. shape_type must be one of:
  "terminator" (oval/stadium, e.g. Başlanğıc / Son),
  "process" (rectangle, e.g. assignments),
  "decision" (diamond, conditions),
  "input_output" (parallelogram, input or output of a variable).
- "text" must contain the label EXACTLY as written, in Azerbaijani \
(e.g. "Başlanğıc", "Son", "a = 3"). Do not translate or correct anything.
- One edge per arrow, in the arrow's direction. Branch labels written on \
arrows (e.g. "Hə", "Yox") go in that edge's "label"; set label to null \
when the arrow has no label.
- Give nodes short ids: "n1", "n2", ... in reading order (top to bottom).
- Include loop-back arrows as ordinary edges pointing at the earlier node.
- The JSON object MUST contain BOTH keys: "nodes" AND "edges". A flowchart
  always has arrows, so "edges" must not be empty or omitted.
"""


def _is_transient(exc: Exception) -> bool:
    """Return True for errors worth retrying (rate limits, 5xx, network)."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _TRANSIENT_HTTP_CODES
    return isinstance(exc, ConnectionError | TimeoutError)


def parse_diagram_graph(raw_output: str) -> DiagramGraph | None:
    """Validate a JSON model response into a DiagramGraph, or None.

    Empty edge labels are normalised to ``None``.
    """
    try:
        graph = DiagramGraph.model_validate_json(raw_output)
    except ValidationError:
        return None
    for edge in graph.edges:
        if edge.label is not None and not edge.label.strip():
            edge.label = None
    if not graph.nodes:
        return None
    return graph


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
            response_schema=DiagramGraph,
            http_options=genai_types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
        )

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        """Ask Gemini to extract the flowchart structure as a DiagramGraph."""
        prompt = _PROMPT
        if context:
            prompt += (
                "\nSurrounding text from the source document (for label "
                f"context only, do not add it to the diagram):\n{context}"
            )
        contents: list[str | genai_types.Part] = [
            genai_types.Part.from_bytes(data=image, mime_type="image/png"),
            prompt,
        ]

        last_error: Exception | None = None
        raw_output = ""
        graph: DiagramGraph | None = None
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
                graph = parse_diagram_graph(raw_output)
                if graph is not None:
                    break
                # Invalid/incomplete JSON is treated like a transient
                # failure: the model often produces a complete graph on a
                # second attempt.
                logger.warning(
                    "Model returned invalid DiagramGraph JSON (attempt %d/%d)",
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
                    "Model response was not a valid DiagramGraph JSON "
                    f"after {1 + _MAX_RETRIES} attempts."
                ),
            )

        return VisionResult(
            raw_model_output=raw_output,
            diagram_graph=graph,
            confidence_note=(
                f"Generated by {self._model_name}; geometry not included — "
                "layout is computed downstream."
            ),
        )
