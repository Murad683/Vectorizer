"""Document ingestion: extract ordered elements from source files."""

from pathlib import Path

from app.core.ingestion.base import Extractor
from app.core.ingestion.docx_extractor import DocxExtractor
from app.core.ingestion.image_extractor import ImageExtractor
from app.core.ingestion.image_set_extractor import ImageSetExtractor, image_names
from app.core.ingestion.pdf_extractor import (
    PageInfo,
    PageSelection,
    PdfExtractor,
    classify_pages,
    count_pdf_pages,
    format_page_list,
    parse_page_list,
    render_thumbnails,
)

_EXTRACTORS: dict[str, type[Extractor]] = {
    ".pdf": PdfExtractor,
    ".docx": DocxExtractor,
    ".png": ImageExtractor,
    ".jpg": ImageExtractor,
    ".jpeg": ImageExtractor,
    # Built by the server from images uploaded together; never uploaded as such.
    ".zip": ImageSetExtractor,
}


def get_extractor(file_path: Path, selection: PageSelection | None = None) -> Extractor:
    """Return the extractor matching the file extension.

    ``selection`` (the pages a user chose) only applies to PDFs.

    Raises:
        ValueError: if the extension has no registered extractor.
    """
    extractor_cls = _EXTRACTORS.get(file_path.suffix.lower())
    if extractor_cls is None:
        supported = ", ".join(sorted(_EXTRACTORS))
        raise ValueError(
            f"Unsupported file type '{file_path.suffix}'. Supported: {supported}"
        )
    if extractor_cls is PdfExtractor:
        return PdfExtractor(selection)
    return extractor_cls()


__all__ = [
    "DocxExtractor",
    "Extractor",
    "ImageExtractor",
    "ImageSetExtractor",
    "PageInfo",
    "PageSelection",
    "PdfExtractor",
    "classify_pages",
    "count_pdf_pages",
    "format_page_list",
    "get_extractor",
    "image_names",
    "parse_page_list",
    "render_thumbnails",
]
