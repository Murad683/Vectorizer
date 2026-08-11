"""Vision provider abstraction.

Nothing outside this package may import a vision vendor SDK directly —
all diagram analysis goes through the ``VisionProvider`` interface so the
provider can be swapped without touching other code.
"""

from app.core.vision.base import VisionProvider
from app.core.vision.gemini_provider import GeminiVisionProvider


def get_vision_provider() -> VisionProvider:
    """Return the configured vision provider (currently always Gemini)."""
    return GeminiVisionProvider()


__all__ = ["GeminiVisionProvider", "VisionProvider", "get_vision_provider"]
