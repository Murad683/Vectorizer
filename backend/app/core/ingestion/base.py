"""Abstract extractor interface for document ingestion."""

from abc import ABC, abstractmethod
from pathlib import Path

from app.models.document import ExtractedElement


class Extractor(ABC):
    """Extracts ordered content elements from a source document.

    Implementations must preserve the reading order of the source file and
    never raise on individual malformed elements — skip them and log
    instead, so one broken image cannot sink a whole document.
    """

    @abstractmethod
    def extract(self, file_path: Path) -> list[ExtractedElement]:
        """Return all content elements of ``file_path`` in reading order."""
