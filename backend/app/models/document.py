"""Pydantic schemas for the document processing pipeline."""

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


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


class VisionResult(BaseModel):
    """Outcome of a vision provider call.

    A failed or malformed model response never raises out of the provider —
    it produces a ``VisionResult`` with ``error`` set, to be handled
    upstream.
    """

    raw_model_output: str = ""
    mermaid_code: str | None = None
    confidence_note: str | None = None
    error: str | None = None
