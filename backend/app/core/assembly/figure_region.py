"""Widen a figure box to the whole figure, judging by the ink on the page.

The vision model sometimes returns a box that covers only part of a figure (a
matrix of patterns without its last column, answer pictures without their
letters). ``crop_figure`` only ever pads that box, so the picture in the result
was cut. This module looks at the page image itself: starting from the model's
box it takes in the neighbouring parts of the figure (separated by small gaps)
and stops at text, so a figure is cut whole and a line of text is not swallowed.

Everything here is a guess that must be safe: on any doubt the model's own box
is returned, and the result is never narrower than the model's box.
"""

import logging
from collections.abc import Sequence

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# The page is analysed at this size (longest side, pixels): fast and less noisy.
_WORK_SIDE = 900
# Parts of a figure that are closer than this share of the page width belong
# together (cells of a matrix ~2%, a matrix and its answer pictures ~7%, answer
# pictures side by side ~11%).
_GAP_SHARE = 0.12
# A piece of ink that lies this much inside a text block is text, not figure.
_TEXT_SHARE = 0.6
# A piece smaller than this share of the page (both ways) is as small as a
# letter. Such pieces join a figure only when closer than _LABEL_GAP_SHARE of the
# page width to it: a label ("A)") yes, a line of handwriting further away no.
_GLYPH_SHARE = 0.04
_LABEL_GAP_SHARE = 0.04
# Growth on every side below this share of the page is not worth a change.
_MIN_GROWTH_SHARE = 0.02
# Give up (keep the model's box) when the result would be too big.
_MAX_PAGE_SHARE = 0.85
_MAX_GROWTH = 6.0
# A page that is mostly dark cannot be judged by its ink.
_MAX_INK_SHARE = 0.6

_Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels


def refine_figure_box(
    image: bytes, box_2d: list[int], others: Sequence[list[int]] = ()
) -> list[int]:
    """The model's ``[ymin, xmin, ymax, xmax]`` (0-1000) box, widened to the figure.

    ``others`` are the boxes of the neighbouring text and table blocks: ink that
    lies inside them is text and is never taken into the figure.
    """
    if len(box_2d) != 4:
        return box_2d
    try:
        return _refine(image, box_2d, others)
    except Exception:  # noqa: BLE001 - a failed guess must not lose the figure
        logger.warning("Could not widen a figure box", exc_info=True)
        return box_2d


def border_ink_share(picture: Image.Image) -> float:
    """Share of dark pixels on the one-pixel border of a cut-out figure.

    A figure cut with its white margin has an almost clean border; ink on the
    border means the cut went through the figure.
    """
    gray = np.asarray(picture.convert("L"))
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    return float((border < 128).mean())


def _share_inside(piece: _Box, area: _Box) -> float:
    """Share of ``piece`` that lies inside ``area``."""
    x0, y0 = max(piece[0], area[0]), max(piece[1], area[1])
    x1, y1 = min(piece[2], area[2]), min(piece[3], area[3])
    size = (piece[2] - piece[0]) * (piece[3] - piece[1])
    if x1 <= x0 or y1 <= y0 or size <= 0:
        return 0.0
    return float((x1 - x0) * (y1 - y0) / size)


def _clear_of_text(
    box: _Box, ink: tuple[int, int, int, int], texts: Sequence[_Box]
) -> _Box:
    """Pull the edges of ``box`` back to the text blocks that reach into it.

    The model's own box may overlap the text block above or below the figure
    (a few pixels of the last line of the question end up in the picture). An
    edge is moved only through a strip that has no figure ink: ``ink`` is the box
    of the figure's ink, and the edge never crosses it.
    """
    x0, y0, x1, y1 = box
    ix0, iy0, ix1, iy1 = ink
    for tx0, ty0, tx1, ty1 in texts:
        across = min(x1, tx1) - max(x0, tx0)  # overlap along x
        along = min(y1, ty1) - max(y0, ty0)  # overlap along y
        if across >= 0.3 * min(x1 - x0, tx1 - tx0):
            if y0 < ty1 <= iy0 and (ty0 + ty1) / 2 < (y0 + y1) / 2:
                y0 = int(ty1)  # a text block above
            if iy1 <= ty0 < y1 and (ty0 + ty1) / 2 > (y0 + y1) / 2:
                y1 = int(ty0)  # a text block below
        if along >= 0.3 * min(y1 - y0, ty1 - ty0):
            if x0 < tx1 <= ix0 and (tx0 + tx1) / 2 < (x0 + x1) / 2:
                x0 = int(tx1)  # a text block on the left
            if ix1 <= tx0 < x1 and (tx0 + tx1) / 2 > (x0 + x1) / 2:
                x1 = int(tx0)  # a text block on the right
    return x0, y0, x1, y1


def _refine(image: bytes, box_2d: list[int], others: Sequence[list[int]]) -> list[int]:
    gray = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return box_2d
    height0, width0 = gray.shape
    scale = min(1.0, _WORK_SIDE / max(height0, width0))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            (max(1, round(width0 * scale)), max(1, round(height0 * scale))),
            interpolation=cv2.INTER_AREA,
        )
    height, width = gray.shape

    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if float(ink.mean()) / 255 > _MAX_INK_SHARE:
        return box_2d

    def pixels(box: list[int]) -> _Box:
        ymin, xmin, ymax, xmax = box
        return (
            round(xmin * width / 1000),
            round(ymin * height / 1000),
            round(xmax * width / 1000),
            round(ymax * height / 1000),
        )

    seed = pixels(box_2d)
    texts = [pixels(other) for other in others if len(other) == 4]

    # Sort the ink into pieces. A piece is "solid" (part of a picture: inside the
    # model's box, or big) or "small" (as small as a letter: a label such as "A)"
    # next to a picture, or a line of handwriting the model left out on purpose).
    # Text the model returned as a block is never taken.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    glyph = _GLYPH_SHARE * max(width, height)
    kind = np.zeros(count, dtype=np.uint8)  # 0 ignore, 1 solid, 2 small
    clean = np.zeros(count, dtype=bool)  # not inside any text block
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if area < 2:
            continue
        piece = (x, y, x + w, y + h)
        is_text = (
            max((_share_inside(piece, t) for t in texts), default=0.0) >= _TEXT_SHARE
        )
        clean[index] = not is_text
        if _share_inside(piece, seed) >= _TEXT_SHARE:
            kind[index] = 1  # the model's own box wins over a text box
        elif is_text:
            continue
        else:
            kind[index] = 2 if (w < glyph and h < glyph) else 1
    solid = np.where(kind[labels] == 1, 255, 0).astype(np.uint8)
    small = np.where(kind[labels] == 2, 255, 0).astype(np.uint8)

    gap = max(3, round(_GAP_SHARE * width))
    joined = cv2.dilate(solid, np.ones((gap, gap), np.uint8))
    _, clusters = cv2.connectedComponents(joined, connectivity=8)

    x0, y0, x1, y1 = seed
    inside = (solid[y0:y1, x0:x1] > 0) & (clusters[y0:y1, x0:x1] > 0)
    wanted = np.unique(clusters[y0:y1, x0:x1][inside])
    if wanted.size == 0:
        return box_2d
    body = (solid > 0) & np.isin(clusters, wanted)
    # Letters (labels) are taken only when they sit right next to the picture.
    near = 2 * max(1, round(_LABEL_GAP_SHARE * width)) + 1  # reach = the gap
    beside = cv2.dilate(body.astype(np.uint8), np.ones((near, near), np.uint8)) > 0
    ys, xs = np.nonzero(body | ((small > 0) & beside))
    if xs.size == 0:
        return box_2d

    grown = (
        min(x0, int(xs.min())),
        min(y0, int(ys.min())),
        max(x1, int(xs.max()) + 1),
        max(y1, int(ys.max()) + 1),
    )
    # A box that is already (nearly) right stays as the model gave it: the usual
    # padding of the cut covers a few pixels.
    growth = max(x0 - grown[0], y0 - grown[1], grown[2] - x1, grown[3] - y1)
    if growth < _MIN_GROWTH_SHARE * max(width, height):
        return box_2d
    seed_area = max(1, (x1 - x0) * (y1 - y0))
    grown_area = (grown[2] - grown[0]) * (grown[3] - grown[1])
    if grown_area > _MAX_PAGE_SHARE * width * height:
        return box_2d
    if grown_area > _MAX_GROWTH * seed_area:
        return box_2d

    # Where the figure's own ink is (not the letters of a text line the model's
    # box happens to cut through): an edge is never pulled back over it.
    pure_y, pure_x = np.nonzero((body | ((small > 0) & beside)) & clean[labels])
    if pure_x.size:
        ink_box = (
            int(pure_x.min()),
            int(pure_y.min()),
            int(pure_x.max()) + 1,
            int(pure_y.max()) + 1,
        )
    else:
        ink_box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    gx0, gy0, gx1, gy1 = _clear_of_text(grown, ink_box, texts)
    refined = [
        max(0, int(gy0 * 1000 / height)),
        max(0, int(gx0 * 1000 / width)),
        min(1000, -int(-gy1 * 1000 // height)),
        min(1000, -int(-gx1 * 1000 // width)),
    ]
    if refined != box_2d:
        logger.info("figure box widened from %s to %s", box_2d, refined)
    return refined
