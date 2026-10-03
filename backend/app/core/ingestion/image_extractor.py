"""Standalone image ingestion: a single PNG/JPEG upload as a one-page document."""

import io
import logging
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.ingestion.base import Extractor
from app.models.document import ElementKind, ExtractedElement

logger = logging.getLogger(__name__)

_EXT_TO_IMAGE_EXT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


class ImageExtractor(Extractor):
    """Treats a standalone PNG/JPEG upload as a single-page document.

    Produces exactly one IMAGE element carrying the whole file, so it
    enters the SAME classify → vision → layout → assembly flow that
    images embedded in PDF/DOCX pages already go through today — no
    separate pipeline. The classifier's ``rule_image_is_diagram`` routes
    any IMAGE element to vision unchanged, regardless of source format.

    Validates the file is actually a decodable image (via Pillow) before
    handing it off — a corrupt file or a spoofed extension (e.g. a text
    file renamed to ``.png``) raises here, exactly like ``PdfExtractor``/
    ``DocxExtractor`` raise on malformed input of their own formats. The
    background worker's existing retry-then-FAIL handling takes it from
    there; this is not a new failure path.
    """

    def extract(self, file_path: Path) -> list[ExtractedElement]:
        """Return a single-element list wrapping the whole image file."""
        raw_bytes = file_path.read_bytes()
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                img.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"Not a valid image file: {file_path.name}") from exc

        image_ext = _EXT_TO_IMAGE_EXT.get(file_path.suffix.lower(), "png")
        logger.info("Extracted 1 image element from %s", file_path.name)
        return [
            ExtractedElement(
                source_page=1,
                element_kind=ElementKind.IMAGE,
                raw_content=raw_bytes,
                metadata={"image_ext": image_ext},
            )
        ]
