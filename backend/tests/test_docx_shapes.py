"""Tests for native DOCX shape generation (single wpc:wpc canvas)."""

import zipfile
from pathlib import Path

import docx
from docx.shared import Emu
from lxml import etree

from app.core.layout import layout_graph
from app.core.render.docx_shapes import add_diagram_to_docx
from app.models.diagram import DiagramGraph

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": ("http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"),
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
}

# ST_RelFromH does NOT include "paragraph" — writing it there is what
# broke the Phase 4 output. Keep the legal value sets pinned in tests.
_VALID_REL_H = {
    "character",
    "column",
    "insideMargin",
    "leftMargin",
    "margin",
    "outsideMargin",
    "page",
    "rightMargin",
}
_VALID_REL_V = {
    "bottomMargin",
    "insideMargin",
    "line",
    "margin",
    "outsideMargin",
    "page",
    "paragraph",
    "topMargin",
}


def _build_docx(flowchart_graph: DiagramGraph, path: Path, **kwargs: str) -> Path:
    layouted = layout_graph(flowchart_graph)
    document = docx.Document()
    add_diagram_to_docx(
        layouted,
        document,
        offset_x_emu=Emu(0),
        offset_y_emu=Emu(0),
        max_width_emu=Emu(int(6.5 * 914400)),
        max_height_emu=Emu(int(8.5 * 914400)),
        **kwargs,  # type: ignore[arg-type]
    )
    document.save(str(path))
    return path


def _body(path: Path) -> etree._Element:
    return docx.Document(str(path)).element.body


class TestDocxCanvas:
    def test_every_package_part_parses(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """Every XML part of the archive must be valid XML (lxml strict)."""
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".xml", ".rels")):
                    etree.fromstring(zf.read(name))  # raises on any error

    def test_single_anchor_with_single_canvas(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        anchors = body.findall(".//wp:anchor", _NS)
        assert len(anchors) == 1
        canvases = body.findall(".//wpc:wpc", _NS)
        assert len(canvases) == 1
        # All shapes live inside the canvas, none outside.
        assert len(body.findall(".//wpc:wpc//wps:wsp", _NS)) == len(
            body.findall(".//wps:wsp", _NS)
        )

    def test_relative_from_values_are_schema_legal(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        for variant, expect_h, expect_v in (
            ("paragraph", "column", "paragraph"),
            ("page", "page", "page"),
        ):
            path = _build_docx(
                flowchart_graph,
                tmp_path / f"diagram_{variant}.docx",
                relative_from=variant,
            )
            anchor = _body(path).find(".//wp:anchor", _NS)
            rel_h = anchor.find("wp:positionH", _NS).get("relativeFrom")
            rel_v = anchor.find("wp:positionV", _NS).get("relativeFrom")
            assert rel_h == expect_h and rel_h in _VALID_REL_H
            assert rel_v == expect_v and rel_v in _VALID_REL_V

    def test_every_node_is_a_native_wsp_with_correct_preset(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        presets = [
            g.get("prst") for g in body.findall(".//wps:wsp/wps:spPr/a:prstGeom", _NS)
        ]
        assert presets.count("roundRect") == 2  # Başlanğıc, Son
        assert presets.count("diamond") == 1  # a < 164
        assert presets.count("parallelogram") == 2  # n, n
        # rect: 3 process nodes + 2 borderless label boxes (Hə, Yox).
        assert presets.count("rect") == 5

    def test_shape_text_is_real_editable_text(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        texts = {t.text for t in body.findall(".//wps:txbx//w:t", _NS) if t.text}
        for node in flowchart_graph.nodes:
            assert node.text in texts  # Azerbaijani preserved in w:t runs
        assert "Hə" in texts and "Yox" in texts

    def test_connectors_are_attached_via_cxn(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        connectors = body.findall(".//wps:wsp[wps:cNvCnPr]", _NS)
        # 7 forward edges + 3 back-edge rail segments.
        assert len(connectors) == 10
        shape_ids = {
            s.find("wps:cNvPr", _NS).get("id")
            for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
        }
        st = body.findall(".//wps:cNvCnPr/a:stCxn", _NS)
        en = body.findall(".//wps:cNvCnPr/a:endCxn", _NS)
        # 7 forward connectors attached at both ends; the back-edge chain
        # only at its outermost ends (first segment start, last segment end).
        assert len(st) == 8
        assert len(en) == 8
        for cxn in st + en:
            assert cxn.get("id") in shape_ids  # references a real sibling

    def test_back_edge_rail_clears_all_shapes(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        rail_segments = [
            w
            for w in body.findall(".//wps:wsp", _NS)
            if w.find("wps:cNvPr", _NS).get("name", "").startswith("backedge")
        ]
        assert len(rail_segments) == 3
        # The vertical rail segment must start right of every node shape.
        node_rights = [
            int(s.find(".//a:off", _NS).get("x"))
            + int(s.find(".//a:ext", _NS).get("cx"))
            for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
            if s.find("wps:cNvPr", _NS).get("name", "").startswith("node-")
        ]
        vertical = next(
            s
            for s in rail_segments
            if s.find("wps:cNvPr", _NS).get("name").endswith("-1")
        )
        rail_x = int(vertical.find(".//a:off", _NS).get("x"))
        assert rail_x > max(node_rights)

    def test_unique_cnvpr_ids(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        ids = [c.get("id") for c in body.findall(".//wps:cNvPr", _NS)]
        assert len(ids) == len(set(ids))
        assert "1" not in ids  # 1 is reserved for the anchor's wp:docPr
