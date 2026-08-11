"""DOCX extraction using python-docx."""

import logging
from pathlib import Path

import docx
from docx.document import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.ingestion.base import Extractor
from app.models.document import ElementKind, ExtractedElement

logger = logging.getLogger(__name__)

# XML namespaces used inside DOCX bodies for embedded image references.
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


class DocxExtractor(Extractor):
    """Extracts paragraphs, tables, and embedded images from DOCX files.

    Walks the document body in order. Paragraphs become text elements;
    images referenced from a paragraph become separate image elements
    placed at that paragraph's position; tables become text elements with
    ``metadata["is_table"] = True`` (cells joined with tabs, rows with
    newlines). DOCX has no page geometry, so ``source_page`` is always 1
    and there are no bounding boxes.
    """

    def extract(self, file_path: Path) -> list[ExtractedElement]:
        """Return all body elements of the DOCX in document order."""
        document = docx.Document(str(file_path))
        elements: list[ExtractedElement] = []
        for child in document.element.body.iterchildren():
            if child.tag == f"{{{_NS['w']}}}p":
                paragraph = Paragraph(child, document)
                elements.extend(self._paragraph_images(paragraph, document))
                if paragraph.text.strip():
                    elements.append(
                        ExtractedElement(
                            source_page=1,
                            element_kind=ElementKind.TEXT_BLOCK,
                            raw_content=paragraph.text.strip(),
                            metadata={
                                "is_table": False,
                                "style": (
                                    paragraph.style.name if paragraph.style else None
                                ),
                            },
                        )
                    )
            elif child.tag == f"{{{_NS['w']}}}tbl":
                table = Table(child, document)
                elements.append(self._table_element(table))
        logger.info("Extracted %d elements from DOCX %s", len(elements), file_path.name)
        return elements

    @staticmethod
    def _paragraph_images(
        paragraph: Paragraph, document: Document
    ) -> list[ExtractedElement]:
        """Return image elements embedded in the given paragraph."""
        images: list[ExtractedElement] = []
        blips = paragraph._p.findall(f".//{{{_NS['a']}}}blip")
        for blip in blips:
            rel_id = blip.get(f"{{{_NS['r']}}}embed")
            if not rel_id:
                continue
            try:
                image_part = document.part.rels[rel_id].target_part
            except KeyError:
                logger.warning("Missing image relationship %s; skipped", rel_id)
                continue
            images.append(
                ExtractedElement(
                    source_page=1,
                    element_kind=ElementKind.IMAGE,
                    raw_content=image_part.blob,
                    metadata={
                        "image_ext": image_part.partname.ext,
                        "rel_id": rel_id,
                    },
                )
            )
        return images

    @staticmethod
    def _table_element(table: Table) -> ExtractedElement:
        """Flatten a DOCX table into a tab/newline-delimited text element."""
        rows = [
            "\t".join(cell.text.strip() for cell in row.cells) for row in table.rows
        ]
        return ExtractedElement(
            source_page=1,
            element_kind=ElementKind.TEXT_BLOCK,
            raw_content="\n".join(rows),
            metadata={
                "is_table": True,
                "n_rows": len(table.rows),
                "n_cols": len(table.columns),
            },
        )
