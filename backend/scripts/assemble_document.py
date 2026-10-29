"""Phase 5 validation: assemble the full sample document end to end.

Runs extract → classify → (vision → layout → shapes for each diagram) →
assembly on the real sample and writes samples/output/full_document.docx
and full_document.pptx. Requires GEMINI_API_KEY in backend/.env.

Usage (from backend/):
    venv/Scripts/python scripts/assemble_document.py
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lxml import etree  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.assembly import assemble_docx, assemble_pptx  # noqa: E402
from app.core.classifier import classify_elements  # noqa: E402
from app.core.ingestion import get_extractor  # noqa: E402
from app.core.vision.cache import CachedVisionProvider  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
OUTPUT = SAMPLES / "output"


def validate_package(path: Path) -> int:
    """lxml-parse every XML part of an OOXML package; return failures."""
    failures = 0
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                try:
                    etree.fromstring(zf.read(name))
                except etree.XMLSyntaxError as exc:
                    failures += 1
                    print(f"  PARSE FAIL {name}: {exc}")
    return failures


def main() -> int:
    """Assemble the full sample document into DOCX and PPTX."""
    configure_logging(get_settings().log_level)
    OUTPUT.mkdir(exist_ok=True)

    source = SAMPLES / "informatika_seh_40_41.docx"
    classified = classify_elements(get_extractor(source).extract(source))

    vision = CachedVisionProvider()  # one vision call per image, both outputs
    document = assemble_docx(classified, vision_provider=vision)
    docx_path = OUTPUT / "full_document.docx"
    document.save(str(docx_path))
    print(f"Saved {docx_path}")

    prs = assemble_pptx(classified, vision_provider=vision)
    pptx_path = OUTPUT / "full_document.pptx"
    prs.save(str(pptx_path))
    print(f"Saved {pptx_path}")

    failures = validate_package(docx_path) + validate_package(pptx_path)
    if failures:
        print(f"PACKAGE VALIDATION FAILED: {failures} bad parts")
        return 1
    print("Package validation: every XML part in both files parses cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
