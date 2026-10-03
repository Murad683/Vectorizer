"""Document ingestion: extract ordered elements from source files."""

from pathlib import Path

from app.core.ingestion.base import Extractor
from app.core.ingestion.docx_extractor import DocxExtractor
from app.core.ingestion.image_extractor import ImageExtractor
from app.core.ingestion.pdf_extractor import PdfExtractor

_EXTRACTORS: dict[str, type[Extractor]] = {
    ".pdf": PdfExtractor,
    ".docx": DocxExtractor,
    ".png": ImageExtractor,
    ".jpg": ImageExtractor,
    ".jpeg": ImageExtractor,
}


def get_extractor(file_path: Path) -> Extractor:
    """Return the extractor matching the file extension.

    Raises:
        ValueError: if the extension has no registered extractor.
    """
    extractor_cls = _EXTRACTORS.get(file_path.suffix.lower())
    if extractor_cls is None:
        supported = ", ".join(sorted(_EXTRACTORS))
        raise ValueError(
            f"Unsupported file type '{file_path.suffix}'. Supported: {supported}"
        )
    return extractor_cls()


__all__ = [
    "DocxExtractor",
    "Extractor",
    "ImageExtractor",
    "PdfExtractor",
    "get_extractor",
]
