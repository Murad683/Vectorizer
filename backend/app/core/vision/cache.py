"""One vision call per distinct image within a job.

A job produces both a DOCX and a PPTX, and each assembler asks the vision
provider about every diagram image. Without sharing, every image was sent
to Gemini twice (and a structural retry ran twice too). This wrapper is
created once per job and handed to both assemblers: the first request for
an image goes to the real provider, later ones get a copy of that result.
"""

import hashlib
import logging
from collections.abc import Callable

from app.core.vision.base import VisionProvider
from app.models.document import VisionResult

logger = logging.getLogger(__name__)


def _default_factory() -> VisionProvider:
    from app.core.vision import get_vision_provider  # avoid an import cycle

    return get_vision_provider()


class CachedVisionProvider(VisionProvider):
    """Memoises ``analyze_diagram`` by image content.

    The real provider is created lazily on the first call, so a job without
    any diagram never needs an API key. Errors are cached too: both output
    files then show the same fallback, and a failing image is not paid for
    twice. The context text is part of the prompt only as a hint, so the
    image alone is the cache key.
    """

    def __init__(
        self,
        inner: VisionProvider | None = None,
        factory: Callable[[], VisionProvider] = _default_factory,
    ) -> None:
        self._inner = inner
        self._factory = factory
        self._results: dict[str, VisionResult] = {}
        self.calls = 0  # requests that reached the real provider

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        """Return the cached result for ``image``, analysing it once."""
        key = hashlib.sha256(image).hexdigest()
        cached = self._results.get(key)
        if cached is None:
            if self._inner is None:
                self._inner = self._factory()
            cached = self._inner.analyze_diagram(image, context=context)
            self._results[key] = cached
            self.calls += 1
        else:
            logger.info("Vision result reused for image %s", key[:8])
        # Assemblers may adjust what they get; never share one object.
        return cached.model_copy(deep=True)
