"""The HTML report of a gap check: the source with its blocks and gaps marked."""

import base64
import html
import io
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from app.eval.gaps import ERROR, Gap
from app.models.page import BlockKind, PageAnalysis

_KIND_COLOUR = {
    BlockKind.TEXT: "#2e9e4f",
    BlockKind.FIGURE: "#2a6fdb",
    BlockKind.TABLE: "#e08a1e",
    BlockKind.FLOWCHART: "#8a4fd6",
}
_GAP_COLOUR = "#e5202a"


@dataclass
class Row:
    """One page of one run of one sample."""

    sample: str
    run: str
    page_number: int
    image: bytes | None
    page: PageAnalysis | None
    gaps: list[Gap] = field(default_factory=list)
    note: str = ""

    @property
    def errors(self) -> int:
        return sum(1 for g in self.gaps if g.severity == ERROR)

    @property
    def reviews(self) -> int:
        return sum(1 for g in self.gaps if g.severity != ERROR)


def annotate(image: bytes, page: PageAnalysis | None, gaps: list[Gap]) -> bytes:
    """The source picture with block boxes (thin) and gaps (thick red) drawn on it."""
    with Image.open(io.BytesIO(image)) as source:
        picture = source.convert("RGB")
    width, height = picture.size
    draw = ImageDraw.Draw(picture)

    def rect(box: tuple[int, int, int, int], colour: str, thickness: int) -> None:
        ymin, xmin, ymax, xmax = box
        draw.rectangle(
            (
                xmin * width / 1000,
                ymin * height / 1000,
                xmax * width / 1000,
                ymax * height / 1000,
            ),
            outline=colour,
            width=thickness,
        )

    if page is not None:
        for block in page.blocks:
            if block.box_2d is not None and len(block.box_2d) == 4:
                ymin, xmin, ymax, xmax = block.box_2d
                rect((ymin, xmin, ymax, xmax), _KIND_COLOUR[block.kind], 1)
    for gap in gaps:
        if gap.box is not None:
            rect(gap.box, _GAP_COLOUR, 4 if gap.severity == ERROR else 2)
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def build_report(rows: list[Row], title: str = "Gap check") -> str:
    """One HTML page: a summary table, how often each gap kind appears, every page."""
    kinds: dict[str, set[tuple[str, str]]] = {}
    runs_per_sample: dict[str, set[str]] = {}
    for row in rows:
        runs_per_sample.setdefault(row.sample, set()).add(row.run)
        for gap in row.gaps:
            if gap.severity == ERROR:
                kinds.setdefault(gap.kind, set()).add((row.sample, row.run))

    summary = "".join(
        f"<tr><td>{html.escape(r.sample)}</td><td>{html.escape(r.run)}</td>"
        f"<td>{r.page_number}</td>"
        f"<td class='{'bad' if r.errors else 'ok'}'>{r.errors}</td>"
        f"<td>{r.reviews}</td><td>{html.escape(r.note)}</td></tr>"
        for r in rows
    )
    frequency = (
        "".join(
            f"<tr><td>{html.escape(kind)}</td><td>{len(hits)}</td></tr>"
            for kind, hits in sorted(kinds.items(), key=lambda item: -len(item[1]))
        )
        or "<tr><td colspan='2'>no errors</td></tr>"
    )

    cards = []
    for row in rows:
        gaps = (
            "".join(
                f"<li class='{'bad' if g.severity == ERROR else 'rev'}'>"
                f"<b>{html.escape(g.severity)}</b> {html.escape(g.kind)}: "
                f"{html.escape(g.detail)}</li>"
                for g in row.gaps
            )
            or "<li class='ok'>no gaps</li>"
        )
        picture = (
            f"<img src='{_data_uri(annotate(row.image, row.page, row.gaps))}' "
            "alt='page'>"
            if row.image is not None
            else "<p>no picture</p>"
        )
        cards.append(
            f"<section><h3>{html.escape(row.sample)} / {html.escape(row.run)} / "
            f"page {row.page_number}"
            f" &mdash; {row.errors} error, {row.reviews} to review</h3>"
            f"<div class='pair'>{picture}<ul>{gaps}</ul></div></section>"
        )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font:14px system-ui,sans-serif;margin:24px;background:#fafafa;color:#222}"
        "table{border-collapse:collapse;margin:8px 0 24px}"
        "td,th{border:1px solid #ccc;padding:4px 10px}"
        ".bad{color:#c4141d;font-weight:600}.ok{color:#2e7d32}.rev{color:#9a6a00}"
        ".pair{display:flex;gap:20px;align-items:flex-start}"
        "img{max-width:520px;border:1px solid #bbb}"
        "ul{padding-left:18px;max-width:560px}section{margin-bottom:34px}"
        ".legend span{display:inline-block;padding:1px 8px;margin-right:6px;"
        "color:#fff;border-radius:3px}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        "<p class='legend'><span style='background:#2e9e4f'>text</span>"
        "<span style='background:#2a6fdb'>figure</span>"
        "<span style='background:#e08a1e'>table</span>"
        "<span style='background:#8a4fd6'>flowchart</span>"
        "<span style='background:#e5202a'>gap</span></p>"
        "<h2>Errors by kind (in how many sample runs)</h2>"
        f"<table><tr><th>kind</th><th>sample runs</th></tr>{frequency}</table>"
        "<h2>Every page</h2><table><tr><th>sample</th><th>run</th><th>page</th>"
        f"<th>errors</th><th>review</th><th>note</th></tr>{summary}</table>"
        f"{''.join(cards)}</body></html>"
    )
