"""Classical computer-vision geometry for flowchart images.

The vision LLM reads text and topology well but its coordinates are
unreliable (boxes drift by half a symbol or land on the wrong symbol).
This module measures geometry deterministically, at pixel accuracy and
with no API cost:

- **Shapes**: every flowchart symbol is a closed outline, so its interior
  is a white region not connected to the page background. Those regions
  (filtered by size) are the symbols; their bounding boxes are exact.
- **Guide segments**: the straight horizontal/vertical connector lines of
  the original drawing, taken only from ink that is connected to a symbol
  outline (so titles, watermarks and free text are ignored). The router
  prefers to follow them, which reproduces the author's routing — lines
  that already avoid text and each other.

``annotate`` draws numbered marks on the detected shapes so the LLM can
refer to symbols by number ("set-of-mark" prompting) instead of guessing
coordinates.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
from numpy.typing import NDArray

from app.models.diagram import ShapeType

if TYPE_CHECKING:
    from app.models.diagram import DiagramGraph

logger = logging.getLogger(__name__)

_Image = NDArray[Any]  # OpenCV returns loosely typed arrays

_WORK_SIZE = 1200  # images are normalised so the longer side is this many px
_MIN_SHAPE_W = 0.03  # fraction of the work image width
_MIN_SHAPE_H = 0.012
_MIN_SHAPE_AREA = 0.0008
_MAX_SHAPE_AREA = 0.25
_LINE_MIN_LEN = 0.045  # fraction of the longer side; shorter = likely text strokes
_OUTLINE_PAD = 2  # work px added around an interior box to cover its outline
_GAP_CLOSE = 7  # closing kernel (work px) that seals small breaks in outlines


@dataclass
class DetectedShape:
    """A closed symbol found in the image (original-image pixel coords)."""

    x0: float
    y0: float
    x1: float
    y1: float
    fill: float  # interior area / bbox area: ~1 rect, ~0.8 parallelogram, ~0.5 diamond
    # Geometric hint where the outline is unambiguous: "document" (flat top,
    # vertical sides, wavy bottom); "" = no opinion (the model decides).
    kind: str = ""


@dataclass
class Detection:
    """Geometry measured from one image."""

    width: int
    height: int
    shapes: list[DetectedShape] = field(default_factory=list)
    # Straight connector segments (x0, y0, x1, y1), original-image pixels.
    segments: list[tuple[float, float, float, float]] = field(default_factory=list)


def _binarise(gray: _Image) -> _Image:
    """Ink mask (255 = ink) robust to grey lines and dark page borders."""
    block = max(15, (min(gray.shape) // 30) | 1)
    ink = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 12
    )
    # Large solid dark areas (letterbox bars, photos) are not drawing ink.
    _, dark = cv2.threshold(gray, 60, 255, cv2.THRESH_BINARY_INV)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    area_limit = 0.02 * gray.shape[0] * gray.shape[1]
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        # Only SOLID dark blocks (bars, photos): a drawing made of thin
        # black lines can be large but is mostly empty inside its bbox.
        if area > area_limit and area > 0.6 * w * h:
            ink[lab == i] = 0
    return cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


# (x, y, w, h, fill, kind) in work px.
_Shape = tuple[int, int, int, int, float, str]


def _outline_kind(solid: _Image) -> str:
    """Classify a filled symbol mask: "document" or "" (undecided).

    A document symbol has a flat top, vertical sides and a wave along the
    bottom whose left part sits clearly lower than its right part. Medians
    over wide column bands ignore local notches (an arrowhead touching the
    outline), which a plain min/max would not.
    """
    h, w = solid.shape
    if h < 8 or w < 8:
        return ""
    present = solid > 0
    cols = np.arange(int(0.08 * w), int(0.92 * w))
    rows = np.arange(int(0.1 * h), int(0.6 * h))
    top = np.argmax(present[:, cols], axis=0)
    bottom = h - 1 - np.argmax(present[::-1, :], axis=0)
    left = np.argmax(present[rows, :], axis=1)
    right = w - 1 - np.argmax(present[rows, ::-1], axis=1)
    if np.ptp(top) > 0.04 * h or np.ptp(left) > 0.04 * w or np.ptp(right) > 0.04 * w:
        return ""
    low = float(np.median(bottom[int(0.1 * w) : int(0.35 * w)]))
    high = float(np.median(bottom[int(0.7 * w) : int(0.92 * w)]))
    return "document" if abs(low - high) >= 0.08 * h else ""


def _find_shapes(ink: _Image) -> list[_Shape]:
    """Interior regions enclosed by ink: (x, y, w, h, fill, kind) in work px."""
    h_img, w_img = ink.shape
    n, lab, stats, _ = cv2.connectedComponentsWithStats(255 - ink, connectivity=4)
    found: list[_Shape] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if x == 0 or y == 0 or x + w >= w_img or y + h >= h_img:
            continue  # touches the border: page background
        if w < _MIN_SHAPE_W * w_img or h < _MIN_SHAPE_H * h_img:
            continue
        if not (
            _MIN_SHAPE_AREA * w_img * h_img <= area <= _MAX_SHAPE_AREA * w_img * h_img
        ):
            continue
        mask = (lab[y : y + h, x : x + w] == i).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        c_area = cv2.contourArea(contour)
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        if hull_area <= 0 or c_area / hull_area < 0.85:
            continue  # flowchart symbols are convex; loop enclosures are not
        fill = c_area / float(w * h)
        kind = ""
        if fill > 0.82:
            solid = np.zeros_like(mask)
            cv2.drawContours(solid, [contour], -1, 255, -1)
            kind = _outline_kind(solid)
        found.append((x, y, w, h, fill, kind))
    # Drop regions nested inside another region (e.g. a symbol drawn in a frame).
    keep = []
    for a in found:
        inside = any(
            b is not a
            and b[0] <= a[0]
            and b[1] <= a[1]
            and a[0] + a[2] <= b[0] + b[2]
            and a[1] + a[3] <= b[1] + b[3]
            for b in found
        )
        if not inside:
            keep.append(a)
    keep.sort(key=lambda s: (s[1], s[0]))
    return keep


def _overlaps(a: _Shape, b: _Shape) -> bool:
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy > 0.2 * min(a[2] * a[3], b[2] * b[3])


def _reach(ink: _Image, x: int, y: int, dx: int, dy: int, limit: int) -> int:
    """How many white pixels lie from (x, y) towards (dx, dy) before ink."""
    h_img, w_img = ink.shape
    steps = 0
    while steps < limit:
        nx, ny = x + dx * (steps + 1), y + dy * (steps + 1)
        if not (0 <= nx < w_img and 0 <= ny < h_img) or ink[ny, nx]:
            break
        steps += 1
    return steps


def _find_gapped_shapes(ink: _Image, found: list[_Shape]) -> list[_Shape]:
    """Symbols whose outline has a small break (a faint or JPEG-broken
    corner, typically a diamond tip where a connector leaves), so their
    interior leaks into the page background and ``_find_shapes`` misses
    them. A stronger closing seals the break; closing also fills the narrow
    tips, so each recovered box is re-measured on the original ink along
    its centre lines (capped, so a scan through the break cannot run off).
    """
    k = _GAP_CLOSE
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    extra: list[_Shape] = []
    # A recovered symbol is a real symbol with a broken outline, so it is in
    # the same size class as the intact ones (not a sliver between arrows).
    min_area = 0.4 * float(np.median([s[2] * s[3] for s in found])) if found else 0.0
    for cand in _find_shapes(closed):
        if any(_overlaps(cand, s) for s in found) or cand[2] * cand[3] < min_area:
            continue
        x, y, w, h, fill, kind = cand
        cx, cy = x + w // 2, y + h // 2
        limit = 4 * k
        left = _reach(ink, x, cy, -1, 0, limit)
        right = _reach(ink, x + w - 1, cy, 1, 0, limit)
        top = _reach(ink, cx, y, 0, -1, limit)
        bottom = _reach(ink, cx, y + h - 1, 0, 1, limit)
        # Flowchart symbols are mirror-symmetric, and a connector glued to
        # a tip can hide part of it: trust the longer reach on each axis.
        left = right = max(left, right)
        top = bottom = max(top, bottom)
        nw, nh = w + left + right, h + top + bottom
        # The interior area grew with the box; keep the fill ratio honest.
        fill = fill * w * h / float(nw * nh)
        extra.append((x - left, y - top, nw, nh, fill, kind))
        logger.info("Recovered a symbol with a broken outline at %s", (x, y, w, h))
    return extra


def _drop_enclosures(shapes: list[_Shape], ink: _Image) -> list[_Shape]:
    """Remove empty areas walled in by connector lines between symbols.

    Two parallel arrows between two symbols enclose a convex white area
    that looks like a symbol interior. Real symbols never touch each other,
    while such an enclosure always touches (or overlaps) the two or more
    symbols its walls connect. A sliver cut off between an arrow and a
    diamond's slanted side lies mostly inside that diamond's box, or is a
    small EMPTY area (a real empty symbol is as large as its neighbours).
    """
    if not shapes:
        return shapes
    median_area = float(np.median([s[2] * s[3] for s in shapes]))

    def small_and_empty(a: _Shape) -> bool:
        x, y, w, h = a[0] + 3, a[1] + 3, a[2] - 6, a[3] - 6
        if w <= 0 or h <= 0:
            return True
        inked = int(np.count_nonzero(ink[y : y + h, x : x + w]))
        return a[2] * a[3] < 0.4 * median_area and inked < 0.005 * w * h

    pad = 8  # work px: two outlines + a little

    def touches(a: _Shape, b: _Shape) -> bool:
        return (
            a[0] - pad < b[0] + b[2]
            and b[0] - pad < a[0] + a[2]
            and a[1] - pad < b[1] + b[3]
            and b[1] - pad < a[1] + a[3]
        )

    def mostly_inside(a: _Shape, b: _Shape) -> bool:
        ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        return b[2] * b[3] > a[2] * a[3] and ix * iy > 0.5 * a[2] * a[3]

    keep = []
    for a in shapes:
        others = [b for b in shapes if b is not a]
        if (
            sum(touches(a, b) for b in others) >= 2
            or any(mostly_inside(a, b) for b in others)
            or small_and_empty(a)
        ):
            logger.info("Dropped a line-walled enclosure at %s", a[:4])
            continue
        keep.append(a)
    return keep


def _find_segments(
    ink: _Image, shapes: list[_Shape]
) -> list[tuple[float, float, float, float]]:
    """Straight connector segments in work px, from ink touching a symbol."""
    if not shapes:
        return []
    h_img, w_img = ink.shape
    # Keep only ink components that touch a symbol outline. Small gaps
    # (an arrowhead stopping short of a line, JPEG breaks) are bridged by
    # computing connectivity on a slightly dilated copy.
    bridged = cv2.dilate(ink, np.ones((7, 7), np.uint8))
    n, lab = cv2.connectedComponents(bridged, connectivity=8)
    touching: set[int] = set()
    for x, y, w, h, _, _ in shapes:
        x0, y0 = max(x - 3, 0), max(y - 3, 0)
        x1, y1 = min(x + w + 3, w_img), min(y + h + 3, h_img)
        ring = lab[y0:y1, x0:x1].copy()
        ring[3:-3, 3:-3] = 0
        touching.update(int(v) for v in np.unique(ring) if v)
    diagram = (np.isin(lab, list(touching)) & (ink > 0)).astype(np.uint8) * 255
    # Remove the symbols themselves (outline + interior text).
    for x, y, w, h, _, _ in shapes:
        pad = 9
        diagram[max(y - pad, 0) : y + h + pad, max(x - pad, 0) : x + w + pad] = 0
    length = max(8, int(_LINE_MIN_LEN * max(h_img, w_img)))
    segs: list[tuple[float, float, float, float]] = []
    for horizontal in (True, False):
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (length, 1) if horizontal else (1, length)
        )
        lines = cv2.morphologyEx(diagram, cv2.MORPH_OPEN, kernel)
        m, _, stats, _ = cv2.connectedComponentsWithStats(lines, connectivity=8)
        for i in range(1, m):
            x, y, w, h, _ = (int(v) for v in stats[i])
            if horizontal:
                segs.append((x, y + h / 2, x + w, y + h / 2))
            else:
                segs.append((x + w / 2, y, x + w / 2, y + h))
    return segs


def detect(image: bytes) -> Detection | None:
    """Measure symbols and connector lines; None if the image can't be read."""
    data = np.frombuffer(image, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    orig_h, orig_w = img.shape
    factor = float(_WORK_SIZE) / float(max(orig_h, orig_w))
    interp = cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA
    work = cv2.resize(img, None, fx=factor, fy=factor, interpolation=interp)
    ink = _binarise(work)
    shapes = _find_shapes(ink)
    shapes = _drop_enclosures(
        sorted(shapes + _find_gapped_shapes(ink, shapes), key=lambda s: (s[1], s[0])),
        ink,
    )
    segments = _find_segments(ink, shapes)

    def back(v: float) -> float:
        return v / factor

    detection = Detection(width=orig_w, height=orig_h)
    for x, y, w, h, fill, kind in shapes:
        detection.shapes.append(
            DetectedShape(
                back(max(x - _OUTLINE_PAD, 0)),
                back(max(y - _OUTLINE_PAD, 0)),
                back(x + w + _OUTLINE_PAD),
                back(y + h + _OUTLINE_PAD),
                fill,
                kind,
            )
        )
    detection.segments = [
        (back(a), back(b), back(c), back(d)) for a, b, c, d in segments
    ]
    logger.info(
        "Detected %d shapes, %d guide segments", len(detection.shapes), len(segments)
    )
    return detection


def annotate(image: bytes, detection: Detection) -> bytes:
    """PNG of the image with each detected shape outlined and numbered 1..N."""
    data = np.frombuffer(image, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return image
    factor = float(_WORK_SIZE) / float(max(img.shape[:2]))
    img = cv2.resize(
        img,
        None,
        fx=factor,
        fy=factor,
        interpolation=cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA,
    )
    for number, s in enumerate(detection.shapes, start=1):
        x0, y0 = int(s.x0 * factor), int(s.y0 * factor)
        x1, y1 = int(s.x1 * factor), int(s.y1 * factor)
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 255), 2)
        label = str(number)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        tx, ty = max(x0 - tw - 8, 0), max(y0, th + 6)
        cv2.rectangle(img, (tx, ty - th - 6), (tx + tw + 6, ty), (0, 0, 255), -1)
        cv2.putText(
            img,
            label,
            (tx + 3, ty - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
        )
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else image


def apply_detection(graph: "DiagramGraph", detection: Detection) -> None:
    """Replace the LLM's guessed node boxes with measured ones, via marks.

    Every node whose ``mark`` names a detected shape gets that shape's exact
    box. Only when ALL symbols are matched (one-to-one) is the graph flagged
    ``boxes_detected`` and given the original connector lines as routing
    guides; otherwise the remaining LLM boxes are kept and the layout runs
    in its tolerant (approximate-boxes) mode. Open arrow ends (``EXIT``)
    are not symbols and never take a mark.
    """
    graph.image_width = detection.width
    graph.image_height = detection.height
    w, h = float(detection.width), float(detection.height)
    used: set[int] = set()
    matched = 0
    symbols = [n for n in graph.nodes if n.shape_type != ShapeType.EXIT]
    for node in graph.nodes:
        if node.shape_type == ShapeType.EXIT:
            node.mark = None
            continue
        mark = node.mark
        if mark is None or not 1 <= mark <= len(detection.shapes) or mark in used:
            continue
        used.add(mark)
        s = detection.shapes[mark - 1]
        if s.kind == "document" and node.shape_type in (
            ShapeType.PROCESS,
            ShapeType.INPUT_OUTPUT,
        ):
            # The wavy bottom is measured, not guessed; models often call a
            # print block a parallelogram ("output") or a rectangle.
            logger.info("Node %s: outline is a document symbol", node.id)
            node.shape_type = ShapeType.DOCUMENT
        node.box_2d = [
            round(s.y0 / h * 1000),
            round(s.x0 / w * 1000),
            round(s.y1 / h * 1000),
            round(s.x1 / w * 1000),
        ]
        matched += 1
    if symbols and matched == len(symbols):
        graph.boxes_detected = True
        graph.guide_segments = [
            [
                round(x0 / w * 1000),
                round(y0 / h * 1000),
                round(x1 / w * 1000),
                round(y1 / h * 1000),
            ]
            for x0, y0, x1, y1 in detection.segments
        ]
    logger.info("Marks matched %d/%d symbols to detected shapes", matched, len(symbols))
