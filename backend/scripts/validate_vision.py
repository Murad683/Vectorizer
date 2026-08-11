"""One-shot Phase 2 validation: extract → classify → Gemini → Mermaid.

Runs the full first-stage pipeline on the real flowchart sample and prints
the resulting Mermaid code. Requires GEMINI_API_KEY in backend/.env.

Usage (from backend/):
    venv/Scripts/python scripts/validate_vision.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.core.classifier import classify_elements  # noqa: E402
from app.core.ingestion import get_extractor  # noqa: E402
from app.core.vision import get_vision_provider  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.models.document import ElementKind, ElementType  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
FLOWCHART = (SAMPLES / "flowchart_a3.png").read_bytes()


def main() -> int:
    """Run the extraction → classification → vision chain once."""
    configure_logging(get_settings().log_level)

    source = SAMPLES / "informatika_seh_40_41.docx"
    elements = get_extractor(source).extract(source)
    classified = classify_elements(elements)

    # Locate the flowchart diagram and the question text right before it.
    flowchart_idx = next(
        i for i, c in enumerate(classified) if c.element.raw_content == FLOWCHART
    )
    item = classified[flowchart_idx]
    assert item.element_type == ElementType.DIAGRAM
    context = next(
        (
            str(c.element.raw_content)
            for c in reversed(classified[:flowchart_idx])
            if c.element.element_kind == ElementKind.TEXT_BLOCK
        ),
        None,
    )
    print(f"Diagram element {item.element.id} (classified by {item.rule})")
    print(f"Context passed to the model: {context!r}\n")

    provider = get_vision_provider()
    image = item.element.raw_content
    assert isinstance(image, bytes)
    result = provider.analyze_diagram(image, context=context)

    if result.error:
        print(f"ERROR: {result.error}")
        print(f"Raw model output:\n{result.raw_model_output}")
        return 1

    print("=== MERMAID OUTPUT " + "=" * 41)
    print(result.mermaid_code)
    print("=" * 60)
    print(f"Confidence note: {result.confidence_note}")
    out_path = Path(__file__).parent / "validation_output.mmd"
    out_path.write_text(result.mermaid_code or "", encoding="utf-8")
    print(f"Saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
