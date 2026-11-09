"""Record the model's answers once, play them back for free.

``RecordingProvider`` wraps a real provider and keeps every answer it gives.
``ReplayProvider`` gives the same answers back without any request, so the rest
of the pipeline (classify, assemble, the gap detectors) can be run again and
again after every change. Answers are stored in a JSON file keyed by the hash of
the image, the same key ``CachedVisionProvider`` uses.
"""

import hashlib
import json
from pathlib import Path

from app.core.assembly import assemble_docx, assemble_pptx
from app.core.classifier import classify_elements
from app.core.ingestion import get_extractor
from app.core.vision.base import VisionProvider
from app.core.vision.cache import CachedVisionProvider
from app.models.document import ContentType, ElementKind, VisionResult
from app.models.page import PageAnalysis


def result_key(image: bytes, page: bool = True) -> str:
    """The key of one answer: the image hash (plus ``:page`` for a page answer)."""
    return hashlib.sha256(image).hexdigest() + (":page" if page else "")


class RecordingProvider(VisionProvider):
    """Passes every request on to ``inner`` and keeps the answers."""

    def __init__(self, inner: VisionProvider) -> None:
        self._inner = inner
        self.results: dict[str, VisionResult] = {}

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        if page:
            result = self._inner.analyze_diagram(image, context=context, page=True)
        else:
            result = self._inner.analyze_diagram(image, context=context)
        self.results[result_key(image, page)] = result.model_copy(deep=True)
        return result


class ReplayProvider(VisionProvider):
    """Gives back recorded answers; an image that was never recorded is an error."""

    def __init__(self, results: dict[str, VisionResult]) -> None:
        self._results = results

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        found = self._results.get(result_key(image, page))
        if found is None:
            return VisionResult(error="no recorded answer for this image")
        return found.model_copy(deep=True)


def save_results(path: Path, results: dict[str, VisionResult]) -> None:
    """Write recorded answers as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value.model_dump(mode="json") for key, value in results.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def load_results(path: Path, page_images: list[bytes]) -> dict[str, VisionResult]:
    """Read recorded answers.

    Besides the file ``save_results`` writes, two single-answer files are
    understood (for a source with exactly one whole-page image): a bare page
    answer (``{"blocks": [...]}``) and a bare ``VisionResult``.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "blocks" in data:
        data = {"content_type": ContentType.PAGE.value, "page": data}
    if isinstance(data, dict) and "content_type" in data:
        if len(page_images) != 1:
            raise ValueError(
                f"{path.name} holds one answer but the source has "
                f"{len(page_images)} pages"
            )
        return {result_key(page_images[0]): VisionResult.model_validate(data)}
    return {key: VisionResult.model_validate(value) for key, value in data.items()}


def page_images(source: Path) -> list[bytes]:
    """The whole-page images of a source file (what is sent to the model)."""
    elements = get_extractor(source).extract(source)
    return [
        e.raw_content
        for e in elements
        if e.element_kind == ElementKind.IMAGE
        and e.metadata.get("whole_page")
        and isinstance(e.raw_content, bytes)
    ]


def run_pipeline(
    source: Path, provider: VisionProvider, out_dir: Path
) -> tuple[Path, Path]:
    """Extract, classify and assemble ``source``; save the DOCX and PPTX."""
    classified = classify_elements(get_extractor(source).extract(source))
    out_dir.mkdir(parents=True, exist_ok=True)
    vision = CachedVisionProvider(inner=provider)
    docx_path = out_dir / "output.docx"
    assemble_docx(classified, vision_provider=vision).save(str(docx_path))
    pptx_path = out_dir / "output.pptx"
    assemble_pptx(classified, vision_provider=vision).save(str(pptx_path))
    return docx_path, pptx_path


def page_of(result: VisionResult | None) -> PageAnalysis | None:
    """The page answer inside a result, if it has one."""
    if result is None or result.page is None:
        return None
    return result.page
