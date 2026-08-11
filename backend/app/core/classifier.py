"""Rule-based element classification. No AI calls, no cost.

The classifier runs each element through an ordered chain of rules; the
first rule that returns an ``ElementType`` wins. To add behaviour later
(e.g. "this image is a photo, not a diagram — embed as-is"), append a new
rule function to ``DEFAULT_RULES`` before the fallback — no restructuring
needed.
"""

import logging
from collections.abc import Callable

from app.models.document import (
    ClassifiedElement,
    ElementKind,
    ElementType,
    ExtractedElement,
)

logger = logging.getLogger(__name__)

# A rule inspects an element and either decides its type or abstains (None).
Rule = Callable[[ExtractedElement], ElementType | None]


def rule_image_is_diagram(element: ExtractedElement) -> ElementType | None:
    """Any image element needs vision processing."""
    if element.element_kind == ElementKind.IMAGE:
        return ElementType.DIAGRAM
    return None


def rule_table_text(element: ExtractedElement) -> ElementType | None:
    """Text extracted from a docx/pdf table is copied with formatting."""
    if element.element_kind == ElementKind.TEXT_BLOCK and element.metadata.get(
        "is_table"
    ):
        return ElementType.TABLE
    return None


def rule_plain_text(element: ExtractedElement) -> ElementType | None:
    """Fallback: any remaining text is copied directly."""
    if element.element_kind == ElementKind.TEXT_BLOCK:
        return ElementType.TEXT
    return None


DEFAULT_RULES: list[Rule] = [
    rule_image_is_diagram,
    rule_table_text,
    rule_plain_text,
]


def classify_elements(
    elements: list[ExtractedElement],
    rules: list[Rule] | None = None,
) -> list[ClassifiedElement]:
    """Assign an ``ElementType`` to each element via the rule chain.

    Args:
        elements: extracted elements in document order.
        rules: optional custom rule chain; defaults to ``DEFAULT_RULES``.

    Returns:
        One ``ClassifiedElement`` per input element, order preserved.
    """
    active_rules = DEFAULT_RULES if rules is None else rules
    classified: list[ClassifiedElement] = []
    for element in elements:
        for rule in active_rules:
            element_type = rule(element)
            if element_type is not None:
                classified.append(
                    ClassifiedElement(
                        element=element,
                        element_type=element_type,
                        rule=rule.__name__,
                    )
                )
                break
        else:
            # Unreachable with the default chain, but a custom chain may
            # abstain everywhere; default to TEXT rather than crash.
            logger.warning("No rule matched element %s; defaulting to TEXT", element.id)
            classified.append(
                ClassifiedElement(
                    element=element,
                    element_type=ElementType.TEXT,
                    rule="fallback_unmatched",
                )
            )
    counts: dict[str, int] = {}
    for item in classified:
        counts[item.element_type.value] = counts.get(item.element_type.value, 0) + 1
    logger.info("Classified %d elements: %s", len(classified), counts)
    return classified
