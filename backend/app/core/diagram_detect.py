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


@dataclass
class DetectedShape:
    """A closed symbol found in the image (original-image pixel coords)."""

    x0: float
    y0: float
    x1: float
    y1: float
    fill: float  # interior area / bbox area: ~1 rect, ~0.8 parallelogram, ~0.5 diamond


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


def _find_shapes(ink: _Image) -> list[tuple[int, int, int, int, float]]:
    """Interior regions enclosed by ink: (x, y, w, h, fill) in work px."""
    h_img, w_img = ink.shape
    n, lab, stats, _ = cv2.connectedComponentsWithStats(255 - ink, connectivity=4)
    found: list[tuple[int, int, int, int, float]] = []
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
        found.append((x, y, w, h, c_area / float(w * h)))
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


def _find_segments(
    ink: _Image, shapes: list[tuple[int, int, int, int, float]]
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
    for x, y, w, h, _ in shapes:
        x0, y0 = max(x - 3, 0), max(y - 3, 0)
        x1, y1 = min(x + w + 3, w_img), min(y + h + 3, h_img)
        ring = lab[y0:y1, x0:x1].copy()
        ring[3:-3, 3:-3] = 0
        touching.update(int(v) for v in np.unique(ring) if v)
    diagram = (np.isin(lab, list(touching)) & (ink > 0)).astype(np.uint8) * 255
    # Remove the symbols themselves (outline + interior text).
    for x, y, w, h, _ in shapes:
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
    segments = _find_segments(ink, shapes)

    def back(v: float) -> float:
        return v / factor

    detection = Detection(width=orig_w, height=orig_h)
    for x, y, w, h, fill in shapes:
        detection.shapes.append(
            DetectedShape(
                back(max(x - _OUTLINE_PAD, 0)),
                back(max(y - _OUTLINE_PAD, 0)),
                back(x + w + _OUTLINE_PAD),
                back(y + h + _OUTLINE_PAD),
                fill,
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
    box. Only when ALL nodes are matched (one-to-one) is the graph flagged
    ``boxes_detected`` and given the original connector lines as routing
    guides; otherwise the remaining LLM boxes are kept and the layout runs
    in its tolerant (approximate-boxes) mode.
    """
    graph.image_width = detection.width
    graph.image_height = detection.height
    w, h = float(detection.width), float(detection.height)
    used: set[int] = set()
    matched = 0
    for node in graph.nodes:
        mark = node.mark
        if mark is None or not 1 <= mark <= len(detection.shapes) or mark in used:
            continue
        used.add(mark)
        s = detection.shapes[mark - 1]
        node.box_2d = [
            round(s.y0 / h * 1000),
            round(s.x0 / w * 1000),
            round(s.y1 / h * 1000),
            round(s.x1 / w * 1000),
        ]
        matched += 1
    if graph.nodes and matched == len(graph.nodes):
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
    logger.info(
        "Marks matched %d/%d nodes to detected shapes", matched, len(graph.nodes)
    )
