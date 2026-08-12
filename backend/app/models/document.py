"""Pydantic schemas for the document processing pipeline."""

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.models.diagram import DiagramGraph
from app.models.text_content import TextContent


class ElementKind(str, Enum):
    """Physical kind of content found in a source document."""

    IMAGE = "image"
    TEXT_BLOCK = "text_block"


class ElementType(str, Enum):
    """Processing category assigned by the classifier."""

    DIAGRAM = "diagram"  # needs vision processing
    TEXT = "text"  # direct copy
    TABLE = "table"  # direct copy, formatting preserved


class BoundingBox(BaseModel):
    """Position of an element on its source page, in points (top-left origin)."""

    x0: float
    y0: float
    x1: float
    y1: float


class ExtractedElement(BaseModel):
    """A single piece of content pulled out of a source document."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    source_page: int = Field(description="1-based page index in the source file.")
    element_kind: ElementKind
    raw_content: str | bytes = Field(
        description="Text content for text blocks; raw image bytes for images."
    )
    bounding_box: BoundingBox | None = Field(
        default=None,
        description="Element position, when the source format provides one.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Extractor-specific hints for downstream stages, e.g. "
            "{'is_table': True} or {'image_ext': 'png'}."
        ),
    )


class ClassifiedElement(BaseModel):
    """An extracted element with its processing category attached."""

    element: ExtractedElement
    element_type: ElementType
    rule: str = Field(
        description="Name of the classifier rule that produced this decision."
    )


class ContentType(str, Enum):
    """What the vision model decided an image actually contains."""

    DIAGRAM = "diagram"  # a flowchart → DiagramGraph
    TEXT_CONTENT = "text_content"  # scanned text/matching lists → TextContent


class VisionResult(BaseModel):
    """Outcome of a vision provider call.

    Exactly one of ``diagram_graph`` / ``text_content`` is set on success,
    per ``content_type``. A failed or malformed model response never
    raises out of the provider — it produces a ``VisionResult`` with
    ``error`` set, to be handled upstream.
    """

    raw_model_output: str = ""
    content_type: ContentType | None = None
    diagram_graph: DiagramGraph | None = None
    text_content: TextContent | None = None
    confidence_note: str | None = None
    error: str | None = None


class VisionAnalysis(BaseModel):
    """Discriminated envelope the vision model fills in ONE call.

    ``content_type`` selects which payload is populated: ``diagram`` for a
    flowchart, ``text_content`` for scanned text/matching lists. Used as
    the Gemini ``response_schema``.
    """

    content_type: ContentType
    diagram: DiagramGraph | None = None
    text_content: TextContent | None = None
