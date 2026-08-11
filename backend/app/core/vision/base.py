"""Abstract vision provider interface."""

from abc import ABC, abstractmethod

from app.models.document import VisionResult


class VisionProvider(ABC):
    """Converts diagram images into structured (Mermaid) representations.

    Implementations must never raise on API or parsing failures — they
    return a ``VisionResult`` with ``error`` set so the pipeline can
    decide upstream how to handle the element (retry, embed as image,
    flag for review).
    """

    @abstractmethod
    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        """Analyze a diagram image and return its Mermaid reconstruction.

        Args:
            image: raw image bytes (PNG/JPEG).
            context: optional surrounding text from the source document,
                to help the model resolve ambiguous labels.
        """
