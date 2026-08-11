"""Classifier tests covering all three rule branches with real sample data."""

from pathlib import Path

from app.core.classifier import classify_elements
from app.core.ingestion.docx_extractor import DocxExtractor
from app.models.document import ElementKind, ElementType, ExtractedElement


class TestRuleBranches:
    """Each branch, exercised with elements from the real sample DOCX."""

    def test_real_sample_covers_all_three_types(self, sample_docx: Path) -> None:
        elements = DocxExtractor().extract(sample_docx)
        classified = classify_elements(elements)
        assert len(classified) == len(elements)
        types = {c.element_type for c in classified}
        assert types == {ElementType.DIAGRAM, ElementType.TABLE, ElementType.TEXT}

    def test_image_elements_become_diagrams(self, sample_docx: Path) -> None:
        elements = DocxExtractor().extract(sample_docx)
        classified = classify_elements(elements)
        for item in classified:
            if item.element.element_kind == ElementKind.IMAGE:
                assert item.element_type == ElementType.DIAGRAM
                assert item.rule == "rule_image_is_diagram"

    def test_table_text_becomes_table(self, sample_docx: Path) -> None:
        elements = DocxExtractor().extract(sample_docx)
        classified = classify_elements(elements)
        tables = [c for c in classified if c.element.metadata.get("is_table")]
        assert tables
        assert all(c.element_type == ElementType.TABLE for c in tables)
        assert all(c.rule == "rule_table_text" for c in tables)

    def test_plain_text_becomes_text(self, sample_docx: Path) -> None:
        elements = DocxExtractor().extract(sample_docx)
        classified = classify_elements(elements)
        plain = [
            c
            for c in classified
            if c.element.element_kind == ElementKind.TEXT_BLOCK
            and not c.element.metadata.get("is_table")
        ]
        assert plain
        assert all(c.element_type == ElementType.TEXT for c in plain)


def test_custom_rule_chain_is_pluggable() -> None:
    """A future rule can be prepended without restructuring the classifier."""

    def rule_photos_are_text(element: ExtractedElement) -> ElementType | None:
        if element.metadata.get("is_photo"):
            return ElementType.TEXT
        return None

    photo = ExtractedElement(
        source_page=1,
        element_kind=ElementKind.IMAGE,
        raw_content=b"\x89PNG",
        metadata={"is_photo": True},
    )
    from app.core.classifier import DEFAULT_RULES

    classified = classify_elements(
        [photo], rules=[rule_photos_are_text, *DEFAULT_RULES]
    )
    assert classified[0].element_type == ElementType.TEXT
    assert classified[0].rule == "rule_photos_are_text"


def test_order_is_preserved(sample_docx: Path) -> None:
    elements = DocxExtractor().extract(sample_docx)
    classified = classify_elements(elements)
    assert [c.element.id for c in classified] == [e.id for e in elements]
