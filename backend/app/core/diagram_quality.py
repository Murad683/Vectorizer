"""Objective visual-quality checks for a laid-out diagram.

``quality_issues`` lists every defect a reader would notice: lines through
symbols or labels, labels on symbols, lines on top of / crossing unrelated
lines, diagonal segments and overlapping symbols. Used by the tests and by
the offline replay tool (``scripts/replay_diagram.py``).
"""

from app.models.diagram import DiagramEdge, LayoutedGraph


def quality_issues(lg: LayoutedGraph) -> list[str]:
    """Every visual defect a reader would notice: lines through symbols or
    labels, labels on symbols, lines on top of / crossing unrelated lines,
    diagonal segments, overlapping symbols."""
    issues: list[str] = []
    labels = []
    for e in lg.edges:
        if e.label and e.label_x is not None and e.label_y is not None:
            w = len(e.label) * 8 + 4
            x = e.label_x + 60 - w if e.label_align_right else e.label_x
            labels.append((e, (x, e.label_y, x + w, e.label_y + 18)))

    def hits(
        a: list[float], b: list[float], r: tuple[float, ...], pad: float = 0
    ) -> bool:
        return (
            min(a[0], b[0]) < r[2] - pad
            and max(a[0], b[0]) > r[0] + pad
            and min(a[1], b[1]) < r[3] - pad
            and max(a[1], b[1]) > r[1] + pad
        )

    def segs(e: DiagramEdge) -> list[tuple[list[float], list[float]]]:
        p = e.edge_path or []
        return list(zip(p, p[1:], strict=False))

    for e in lg.edges:
        name = f"{e.source_id}->{e.target_id}"
        for a, b in segs(e):
            if abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5:
                issues.append(f"diagonal {name}")
            for n in lg.nodes:
                if n.id not in (e.source_id, e.target_id) and hits(
                    a, b, (n.x, n.y, n.x + n.width, n.y + n.height)
                ):
                    issues.append(f"{name} through node {n.id}")
            for le, box in labels:
                if le is not e and hits(a, b, box, 1):
                    issues.append(f"{name} through label {le.label!r}")
    for le, box in labels:
        for n in lg.nodes:
            if hits(
                [box[0], box[1]],
                [box[2], box[3]],
                (n.x, n.y, n.x + n.width, n.y + n.height),
            ):
                issues.append(f"label {le.label!r} over node {n.id}")
    edges = lg.edges
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            if edges[i].target_id == edges[j].target_id:
                continue  # arrows into the same symbol may merge
            for a, b in segs(edges[i]):
                for c, d in segs(edges[j]):
                    va, vc = abs(a[0] - b[0]) < 0.5, abs(c[0] - d[0]) < 0.5
                    if va == vc:
                        k = 0 if va else 1
                        if abs(a[k] - c[k]) < 1:
                            lo = max(min(a[1 - k], b[1 - k]), min(c[1 - k], d[1 - k]))
                            hi = min(max(a[1 - k], b[1 - k]), max(c[1 - k], d[1 - k]))
                            if hi - lo > 1:
                                issues.append(f"overlap {i}/{j}")
                    else:
                        v, h = ((a, b), (c, d)) if va else ((c, d), (a, b))
                        x, y = v[0][0], h[0][1]
                        if (
                            min(h[0][0], h[1][0]) + 0.5
                            < x
                            < max(h[0][0], h[1][0]) - 0.5
                            and min(v[0][1], v[1][1]) + 0.5
                            < y
                            < max(v[0][1], v[1][1]) - 0.5
                        ):
                            issues.append(f"cross {i}/{j}")
    nodes = lg.nodes
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            na, nb = nodes[i], nodes[j]
            if (
                na.x < nb.x + nb.width
                and nb.x < na.x + na.width
                and na.y < nb.y + nb.height
                and nb.y < na.y + na.height
            ):
                issues.append(f"nodes overlap {na.id} {nb.id}")
    return issues
