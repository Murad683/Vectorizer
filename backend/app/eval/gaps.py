"""Find what a page conversion lost, judging by the SOURCE image.

The detectors do not trust the model's own account of the page. They look at the
ink on the source picture and ask which of it the answer explains: a figure, a
table or a flowchart explains the ink inside its box, a text block explains the
ink inside its box, but a block that only holds an option letter ("A)") explains
nothing bigger than the letter itself. What is left is a gap.

``find_gaps`` needs the source image and the page answer; ``check_assembly``
looks into the Word file that was built from the answer; ``check_expected``
compares the text with a hand-written (or generated) expected text.

Every gap has a severity: ``error`` (content is missing or wrong) or ``review``
(a person should look: handwriting the model left out on purpose, a page
number, a figure that touches the edge of the photo, ...).
"""

import difflib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import docx
import numpy as np
from PIL import Image

from app.config import get_settings
from app.core.assembly.figure_region import border_ink_share, refine_figure_box
from app.core.assembly.page_blocks import crop_figure, neighbour_boxes
from app.models.page import BlockKind, PageAnalysis, PageBlock

ERROR = "error"
REVIEW = "review"

# The page is looked at in this size (longest side, pixels).
_WORK_SIDE = 900
# Ink pieces closer than this share of the page width form one gap.
_CLUSTER_GAP_SHARE = 0.03
# A gap smaller than this share of the page is noise; at least ERROR_SHARE is an error.
_MIN_GAP_SHARE = 0.004
_ERROR_GAP_SHARE = 0.01
# A cluster this big is the background of a photo (a table, a dark cover), not content.
_BACKGROUND_SHARE = 0.3
# A piece is explained when this much of it lies inside an explaining box.
_EXPLAINED_SHARE = 0.6
# A piece smaller than this share of the page (both ways) is as small as a letter.
_GLYPH_SHARE = 0.04
# A text block this big (share of the page) with this few characters is suspicious.
_SPARSE_AREA_SHARE = 0.03
_SPARSE_MAX_CHARS = 6
# A clean cut leaves a white margin: more dark pixels than this share on the border
# of a cut figure mean the cut went through it (much more sensitive than the
# production log line, which only tells long lines along the border).
_EDGE_INK_WARN = 0.004
# A block that comes this much (units of 1000) above an earlier one is out of order.
_ORDER_SLACK = 20

# "A)", "b)", "(C)", "D." : a block that holds only an option letter.
_LABEL_ONLY = re.compile(r"^\s*\(?[A-Ea-e]\s*[.)]\s*$")

Box = tuple[int, int, int, int]  # [ymin, xmin, ymax, xmax], units of 1000


@dataclass(frozen=True)
class Gap:
    """One thing the conversion lost or got wrong."""

    kind: str
    severity: str
    detail: str
    box: Box | None = None


def is_label_only(block: PageBlock) -> bool:
    """True for a text block whose whole text is an option letter."""
    return block.kind == BlockKind.TEXT and bool(_LABEL_ONLY.match(block.text))


def _valid_box(block: PageBlock) -> Box | None:
    box = block.box_2d
    if box is None or len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = (max(0, min(1000, int(v))) for v in box)
    if ymax <= ymin or xmax <= xmin:
        return None
    return ymin, xmin, ymax, xmax


def _ink_mask(image: bytes) -> np.ndarray | None:
    gray = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    scale = min(1.0, _WORK_SIDE / max(gray.shape))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            (
                max(1, round(gray.shape[1] * scale)),
                max(1, round(gray.shape[0] * scale)),
            ),
            interpolation=cv2.INTER_AREA,
        )
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if float(ink.mean()) / 255 > 0.6:  # a mostly dark photo cannot be judged by ink
        return None
    return np.asarray(ink)


def _pixels(box: Box, width: int, height: int) -> tuple[int, int, int, int]:
    ymin, xmin, ymax, xmax = box
    return (
        round(xmin * width / 1000),
        round(ymin * height / 1000),
        round(xmax * width / 1000),
        round(ymax * height / 1000),
    )


def _units(x0: int, y0: int, x1: int, y1: int, width: int, height: int) -> Box:
    return (
        max(0, int(y0 * 1000 / height)),
        max(0, int(x0 * 1000 / width)),
        min(1000, -int(-y1 * 1000 // height)),
        min(1000, -int(-x1 * 1000 // width)),
    )


def _share_inside(
    piece: tuple[int, int, int, int], area: tuple[int, int, int, int]
) -> float:
    x0, y0 = max(piece[0], area[0]), max(piece[1], area[1])
    x1, y1 = min(piece[2], area[2]), min(piece[3], area[3])
    size = (piece[2] - piece[0]) * (piece[3] - piece[1])
    if x1 <= x0 or y1 <= y0 or size <= 0:
        return 0.0
    return float((x1 - x0) * (y1 - y0) / size)


def effective_figure_box(
    image: bytes, page: PageAnalysis, block: PageBlock, box: Box
) -> Box:
    """The box the assemblers really cut (the model's, widened like ``crop_figure``)."""
    if not get_settings().figure_refine_enabled:
        return box
    ymin, xmin, ymax, xmax = refine_figure_box(
        image, list(box), neighbour_boxes(page.blocks, block)
    )
    return ymin, xmin, ymax, xmax


def uncovered_ink(image: bytes, page: PageAnalysis) -> list[Gap]:
    """Ink on the source that no block of the answer explains."""
    ink = _ink_mask(image)
    if ink is None:
        return []
    height, width = ink.shape
    glyph = _GLYPH_SHARE * max(width, height)

    full: list[tuple[int, int, int, int]] = []  # explain everything inside
    letters: list[tuple[int, int, int, int]] = []  # explain only a letter
    for block in page.blocks:
        box = _valid_box(block)
        if box is None:
            continue
        if block.kind == BlockKind.FIGURE:
            box = effective_figure_box(image, page, block, box)
        pix = _pixels(box, width, height)
        if block.kind == BlockKind.TEXT:
            if not block.text.strip():
                continue  # an empty text block explains nothing
            (letters if is_label_only(block) else full).append(pix)
        else:
            full.append(pix)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    lost = np.zeros(count, dtype=bool)
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if area < 2:
            continue
        piece = (x, y, x + w, y + h)
        if any(_share_inside(piece, box) >= _EXPLAINED_SHARE for box in full):
            continue
        if (
            w < glyph
            and h < glyph
            and any(_share_inside(piece, box) >= _EXPLAINED_SHARE for box in letters)
        ):
            continue
        lost[index] = True
    if not lost.any():
        return []

    mask = np.where(lost[labels], 255, 0).astype(np.uint8)
    gap = max(3, round(_CLUSTER_GAP_SHARE * width))
    _, clusters = cv2.connectedComponents(
        cv2.dilate(mask, np.ones((gap, gap), np.uint8)), connectivity=8
    )
    gaps: list[Gap] = []
    for cluster in np.unique(clusters[mask > 0]):
        ys, xs = np.nonzero((clusters == cluster) & (mask > 0))
        x0, x1, y0, y1 = (
            int(xs.min()),
            int(xs.max()) + 1,
            int(ys.min()),
            int(ys.max()) + 1,
        )
        w, h = x1 - x0, y1 - y0
        if (h < 0.012 * height and w > 0.5 * width) or (
            w < 0.012 * width and h > 0.5 * height
        ):
            continue  # a page rule or border
        share = w * h / (width * height)
        if share < _MIN_GAP_SHARE or share >= _BACKGROUND_SHARE:
            continue
        # Content cut by the edge of the photo is cut in the source too.
        edge = 0.01 * max(width, height)
        at_edge = x0 <= edge or y0 <= edge or x1 >= width - edge or y1 >= height - edge
        gaps.append(
            Gap(
                "uncovered_ink",
                ERROR if share >= _ERROR_GAP_SHARE and not at_edge else REVIEW,
                f"ink that no block explains ({share * 100:.1f}% of the page"
                + (", at the edge of the page)" if at_edge else ")"),
                _units(x0, y0, x1, y1, width, height),
            )
        )
    return gaps


def empty_options(image: bytes, page: PageAnalysis) -> list[Gap]:
    """Option blocks that hold only their letter ("A) ") and nothing else."""
    ink = _ink_mask(image)
    gaps: list[Gap] = []
    for block in page.blocks:
        if not is_label_only(block):
            continue
        box = _valid_box(block)
        picture_inside = False
        if box is not None and ink is not None:
            height, width = ink.shape
            x0, y0, x1, y1 = _pixels(box, width, height)
            count, _, stats, _ = cv2.connectedComponentsWithStats(
                ink[y0:y1, x0:x1], connectivity=8
            )
            glyph = _GLYPH_SHARE * max(width, height)
            picture_inside = any(
                int(stats[i][2]) >= glyph or int(stats[i][3]) >= glyph
                for i in range(1, count)
                if int(stats[i][4]) >= 2
            )
        gaps.append(
            Gap(
                "empty_option",
                ERROR if picture_inside else REVIEW,
                f"option {block.text.strip()!r} has no content"
                + (" (a picture lies inside its box)" if picture_inside else ""),
                box,
            )
        )
    return gaps


def sparse_text_boxes(image: bytes, page: PageAnalysis) -> list[Gap]:
    """Big text boxes with almost no text: a picture fell into a text block."""
    gaps: list[Gap] = []
    for block in page.blocks:
        if block.kind != BlockKind.TEXT or is_label_only(block):
            continue
        box = _valid_box(block)
        if box is None:
            continue
        ymin, xmin, ymax, xmax = box
        share = (ymax - ymin) * (xmax - xmin) / 1_000_000
        chars = len(block.text.strip())
        if share >= _SPARSE_AREA_SHARE and chars <= _SPARSE_MAX_CHARS:
            gaps.append(
                Gap(
                    "sparse_text_box",
                    REVIEW,
                    f"a text block of {chars} characters covers "
                    f"{share * 100:.1f}% of the page",
                    box,
                )
            )
    return gaps


def cut_figures(image: bytes, page: PageAnalysis) -> list[Gap]:
    """Figures whose cut-out still has ink on its border, as the assemblers cut it."""
    gaps: list[Gap] = []
    for block in page.blocks:
        if block.kind != BlockKind.FIGURE:
            continue
        box = _valid_box(block)
        if box is None:
            gaps.append(
                Gap("figure_cut", ERROR, "a figure block without a usable box", None)
            )
            continue
        png = crop_figure(image, list(box), neighbour_boxes(page.blocks, block))
        if png is None:
            gaps.append(
                Gap("figure_cut", ERROR, "the figure could not be cut out", box)
            )
            continue
        with Image.open(io.BytesIO(png)) as picture:
            share = border_ink_share(picture)
        if share > _EDGE_INK_WARN:
            gaps.append(
                Gap(
                    "figure_cut",
                    REVIEW,
                    f"ink on {share * 100:.0f}% of the border of the cut figure",
                    box,
                )
            )
    return gaps


def block_order(page: PageAnalysis) -> list[Gap]:
    """A block that lies clearly above an earlier block it overlaps sideways."""
    gaps: list[Gap] = []
    boxes = [box for b in page.blocks if (box := _valid_box(b)) is not None]
    for j, later in enumerate(boxes):
        for earlier in boxes[:j]:
            across = min(earlier[3], later[3]) - max(earlier[1], later[1])
            narrow = min(earlier[3] - earlier[1], later[3] - later[1])
            if across >= 0.3 * narrow and later[2] < earlier[0] - _ORDER_SLACK:
                gaps.append(
                    Gap(
                        "order",
                        REVIEW,
                        "a block comes after one that lies below it",
                        later,
                    )
                )
                break
    return gaps


def find_gaps(image: bytes, page: PageAnalysis) -> list[Gap]:
    """All gaps of one page answer that can be found from the source and the answer."""
    return [
        *empty_options(image, page),
        *uncovered_ink(image, page),
        *sparse_text_boxes(image, page),
        *cut_figures(image, page),
        *block_order(page),
    ]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def docx_text(path: Path) -> str:
    """All text of a Word file: paragraphs and table cells."""
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def check_assembly(page: PageAnalysis, docx_path: Path, image: bytes) -> list[Gap]:
    """Look into the built Word file: is what the answer describes really in it?"""
    gaps: list[Gap] = []
    document = docx.Document(str(docx_path))
    body = _norm(docx_text(docx_path))
    for block in page.blocks:
        if block.kind == BlockKind.TEXT and block.text.strip():
            if _norm(block.text) not in body:
                gaps.append(
                    Gap(
                        "dropped_in_assembly",
                        ERROR,
                        f"text is not in the Word file: {block.text.strip()[:40]!r}",
                        _valid_box(block),
                    )
                )
        elif block.kind == BlockKind.TABLE and block.rows:
            for row in block.rows:
                for cell in row:
                    if cell.strip() and _norm(cell) not in body:
                        gaps.append(
                            Gap(
                                "dropped_in_assembly",
                                ERROR,
                                f"table cell is not in the Word file: "
                                f"{cell.strip()[:30]!r}",
                                _valid_box(block),
                            )
                        )
    figures = [
        b
        for b in page.blocks
        if b.kind == BlockKind.FIGURE
        and (box := _valid_box(b)) is not None
        and crop_figure(image, list(box), neighbour_boxes(page.blocks, b)) is not None
    ]
    flowcharts = [b for b in page.blocks if b.kind == BlockKind.FLOWCHART]
    pictures = len(document.inline_shapes)
    with zipfile.ZipFile(docx_path) as archive:
        has_shapes = b"wps:wsp" in archive.read("word/document.xml")
    if pictures < len(figures):
        gaps.append(
            Gap(
                "dropped_in_assembly",
                ERROR,
                f"pictures in the Word file: {pictures} of {len(figures)} figures",
            )
        )
    if flowcharts and not has_shapes and pictures < len(figures) + len(flowcharts):
        gaps.append(
            Gap(
                "dropped_in_assembly",
                ERROR,
                "a flowchart is neither shapes nor a picture",
            )
        )
    return gaps


def check_expected(expected: str, output: str) -> tuple[list[Gap], float]:
    """Expected lines that are missing from ``output``, and how alike the texts are."""
    body = _norm(output)
    gaps = [
        Gap("text_missing", ERROR, f"expected text is missing: {line.strip()[:50]!r}")
        for line in expected.splitlines()
        if line.strip() and _norm(line) not in body
    ]
    ratio = difflib.SequenceMatcher(None, _norm(expected), body).ratio()
    return gaps, ratio
