"""Outline geometry of flowchart symbols shared by layout and renderers.

The document symbol is drawn with OOXML's ``flowChartDocument`` preset in
both Word and PowerPoint; its wavy bottom edge is one cubic Bézier in a
21600-unit box (ECMA-376 presetShapeDefinitions):
``moveTo(0,0) lnTo(w,0) lnTo(w,17322) cubicBezTo((hc,17322),(hc,23922),
(0,20172)) close``. The router needs points ON that curve so arrows leave
or enter the visible outline, not the empty bounding-box corner.
"""

_P0 = (1.0, 17322 / 21600)
_P1 = (0.5, 17322 / 21600)
_P2 = (0.5, 23922 / 21600)
_P3 = (0.0, 20172 / 21600)

# Bottom of the preset's text rectangle, as a fraction of the height.
DOCUMENT_TEXT_BOTTOM = 17322 / 21600


def _bezier(t: float) -> tuple[float, float]:
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (
        a * _P0[0] + b * _P1[0] + c * _P2[0] + d * _P3[0],
        a * _P0[1] + b * _P1[1] + c * _P2[1] + d * _P3[1],
    )


def document_bottom(fx: float) -> float:
    """Height fraction of the wavy bottom edge at width fraction ``fx``."""
    fx = min(max(fx, 0.0), 1.0)
    lo, hi = 0.0, 1.0  # x(t) falls monotonically from 1 to 0
    for _ in range(40):
        mid = (lo + hi) / 2
        if _bezier(mid)[0] > fx:
            lo = mid
        else:
            hi = mid
    return _bezier((lo + hi) / 2)[1]


def document_outline(steps: int = 24) -> list[tuple[float, float]]:
    """Closed outline as unit-box fractions (top-left origin)."""
    pts = [(0.0, 0.0), (1.0, 0.0)]
    pts += [_bezier(i / steps) for i in range(steps + 1)]
    return pts
