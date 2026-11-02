"""Helpers shared by the DOCX and PPTX assemblers for a whole-page result.

Both assemblers write the SAME blocks in the SAME order (text, tables,
flowcharts, figures); only the pagination differs. Figures are cut out of the
source image here, so the page costs no extra vision call.
"""

import io
import logging
import re
from dataclasses import dataclass, field

from PIL import Image

from app.config import get_settings
from app.core.vision.base import VisionProvider
from app.models.document import ClassifiedElement, VisionResult
from app.models.page import BlockKind, PageBlock, TextStyle
from app.models.text_content import TextRun

logger = logging.getLogger(__name__)

# "118. ..." / "7) ..." start a question; "A) ..." starts an answer option.
_QUESTION_START = re.compile(r"^\s*\d+\s*[.)]")
_OPTION_START = re.compile(r"^\s*[A-Ea-e]\s*\)")

# Figures are cut a little wider than the model's box: it often clips the
# option letters ("A)") at the edge. Units of 1000.
_CROP_PAD = 12

# A flowchart slide keeps its question lines and options on the same slide
# only while they leave this much of the slide to the chart.
MAX_GROUPED_TEXT_IN = 3.2


def use_page_mode(item: ClassifiedElement) -> bool:
    """True when this element is a whole page and page mode is switched on."""
    return bool(item.element.metadata.get("whole_page")) and (
        get_settings().page_mode_enabled
    )


def analyze_image(
    provider: VisionProvider, image: bytes, context: str | None, page: bool
) -> VisionResult:
    """Ask the provider; the ``page`` flag is only passed when it is set."""
    if page:
        return provider.analyze_diagram(image, context=context, page=True)
    return provider.analyze_diagram(image, context=context)


def runs_to_dicts(runs: list[TextRun]) -> list[dict[str, str | None]]:
    """Convert page runs to the run-dict convention of the assemblers."""
    return [
        {
            "text": run.text.replace("\r", "").replace("\n", " "),
            "vert_align": (
                "subscript"
                if run.subscript
                else "superscript" if run.superscript else None
            ),
        }
        for run in runs
        if run.text
    ]


def crop_figure(image: bytes, box_2d: list[int] | None) -> bytes | None:
    """Cut a figure out of the page image (PNG bytes), or None if impossible."""
    if box_2d is None or len(box_2d) != 4:
        return None
    try:
        with Image.open(io.BytesIO(image)) as img:
            width, height = img.size
            ymin, xmin, ymax, xmax = box_2d
            ymin, xmin = max(0, ymin - _CROP_PAD), max(0, xmin - _CROP_PAD)
            ymax, xmax = min(1000, ymax + _CROP_PAD), min(1000, xmax + _CROP_PAD)
            left = int(xmin / 1000 * width)
            top = int(ymin / 1000 * height)
            right = min(width, max(left + 1, int(xmax / 1000 * width)))
            bottom = min(height, max(top + 1, int(ymax / 1000 * height)))
            if right - left < 4 or bottom - top < 4:
                return None
            buffer = io.BytesIO()
            img.convert("RGB").crop((left, top, right, bottom)).save(
                buffer, format="PNG"
            )
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a broken figure must not sink the page
        logger.warning("Could not cut a figure out of the page image")
        return None


def fit_inches(png: bytes, max_w_in: float, max_h_in: float) -> tuple[float, float]:
    """(width, height) in inches inside the limits, proportions kept, no upscale
    beyond 1 px = 1/96 in."""
    with Image.open(io.BytesIO(png)) as img:
        width_px, height_px = img.size
    natural_w = width_px / 96
    natural_h = height_px / 96
    scale = min(max_w_in / natural_w, max_h_in / natural_h, 1.0)
    return natural_w * scale, natural_h * scale


@dataclass
class FlowUnit:
    """A flowchart with the question lines above it and the options below."""

    chart: PageBlock
    before: list[PageBlock] = field(default_factory=list)
    after: list[PageBlock] = field(default_factory=list)


def group_blocks(blocks: list[PageBlock]) -> list[PageBlock | FlowUnit]:
    """Group each flowchart with its question lines and answer options.

    Text blocks directly above a flowchart, from the closest block that starts
    a question, and the option lines directly below it belong to the chart:
    the PPTX puts them on the chart's slide. Everything else keeps its order.
    """
    consumed: set[int] = set()
    units: dict[int, FlowUnit] = {}
    for index, block in enumerate(blocks):
        if block.kind != BlockKind.FLOWCHART:
            continue
        start = index
        probe = index - 1
        while (
            probe >= 0
            and blocks[probe].kind == BlockKind.TEXT
            and probe not in consumed
        ):
            if _QUESTION_START.match(blocks[probe].text) or (
                blocks[probe].style == TextStyle.HEADING
            ):
                start = probe
                break
            probe -= 1
        end = index
        probe = index + 1
        # Everything up to the next question belongs to the chart: its answer
        # options and a caption such as "Sxem 3. ...".
        while (
            probe < len(blocks)
            and blocks[probe].kind == BlockKind.TEXT
            and not _QUESTION_START.match(blocks[probe].text)
            and blocks[probe].style != TextStyle.HEADING
        ):
            end = probe
            probe += 1
        units[start] = FlowUnit(
            chart=block,
            before=list(blocks[start:index]),
            after=list(blocks[index + 1 : end + 1]),
        )
        consumed.update(range(start, end + 1))
    ordered: list[PageBlock | FlowUnit] = []
    for index, block in enumerate(blocks):
        if index in units:
            ordered.append(units[index])
        elif index not in consumed:
            ordered.append(block)
    return ordered
