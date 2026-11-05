"""Several images that belong to one document, read as one page each."""

import logging
import zipfile
from pathlib import Path

from app.core.ingestion.base import Extractor
from app.core.ingestion.image_extractor import image_element
from app.models.document import ExtractedElement

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def image_names(file_path: Path) -> list[str]:
    """Names of the images inside the set, in page order (by member name)."""
    with zipfile.ZipFile(file_path) as archive:
        return sorted(info.filename for info in archive.infolist() if not info.is_dir())


class ImageSetExtractor(Extractor):
    """Reads a ``.zip`` the server built from images uploaded together.

    Members are named ``0001.jpg``, ``0002.png``, ... so their name order is the
    page order the owner chose. Every member goes through the same one-image
    path as a single uploaded image (``image_element``): its bytes are not
    changed, so each page is read exactly like a standalone image would be.
    The zip is never uploaded by a user; the API only builds it from images
    it has already checked.
    """

    def extract(self, file_path: Path) -> list[ExtractedElement]:
        """Return one whole-page IMAGE element per member, in page order."""
        try:
            names = image_names(file_path)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Not a valid image set: {file_path.name}") from exc
        if not names:
            raise ValueError(f"The image set is empty: {file_path.name}")
        elements: list[ExtractedElement] = []
        with zipfile.ZipFile(file_path) as archive:
            for page_number, name in enumerate(names, start=1):
                if Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
                    raise ValueError(f"Not an image in the set: {name}")
                elements.append(image_element(archive.read(name), name, page_number))
        logger.info("Extracted %d images from %s", len(elements), file_path.name)
        return elements
